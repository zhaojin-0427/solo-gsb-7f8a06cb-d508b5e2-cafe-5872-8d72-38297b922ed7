"""存储层：SQLite 元数据 + 内容寻址的本地文件目录。

目录布局（DATA_DIR 下）：
  db.sqlite3          元数据（版本、清单、当前指针/修订号）
  blobs/<digest>/     不可变版本内容（与普通文件一一对应）
  staging/<uuid>/     上传过程中的暂存目录（提交后原子改名到 blobs/）
  recovery.lock       启动恢复互斥锁（多进程共享数据目录）

版本状态：
  available  正常可读/可激活/可回收
  recycling  回收中：不可再激活（409）、新读取返回 410；删除完成后行被移除

不变式：
- 版本行仅在内容已就位于 blobs/ 后才提交，失败绝不产生可见版本；
- 回收开始（状态改为 recycling）与实际删除都由启动恢复兜底完成；
- 所有写事务使用 BEGIN IMMEDIATE，两个服务进程共用目录时序列化生效。
"""

from __future__ import annotations

import fcntl
import os
import shutil
import sqlite3
import time
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager

from .package import PackageError, unpack_and_validate

AVAILABLE = "available"
RECYCLING = "recycling"

# 崩溃残留暂存/孤儿 blob 的默认宽限秒数
DEFAULT_GRACE_SECONDS = 60.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    current_version TEXT,
    revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    digest TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    recycled_at REAL
);
CREATE TABLE IF NOT EXISTS files (
    digest TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    PRIMARY KEY (digest, path)
);
"""


class StoreError(Exception):
    """业务错误，code 为 HTTP 状态码。"""

    def __init__(self, code: int, detail: str, *, revision: int | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.revision = revision


def _is_hex64(digest: str) -> bool:
    return len(digest) == 64 and all(c in "0123456789abcdef" for c in digest.lower())


class Store:
    def __init__(self, data_dir: str):
        self.data_dir = os.path.abspath(data_dir)
        self.blobs_dir = os.path.join(self.data_dir, "blobs")
        self.staging_dir = os.path.join(self.data_dir, "staging")
        self.db_path = os.path.join(self.data_dir, "db.sqlite3")
        self.lock_path = os.path.join(self.data_dir, "recovery.lock")
        os.makedirs(self.blobs_dir, exist_ok=True)
        os.makedirs(self.staging_dir, exist_ok=True)
        self._init_db()

    # ---------- 基础 ----------
    def _connect(self) -> sqlite3.Connection:
        # journal_mode=WAL 是写操作且不受 busy_timeout 保护：
        # 两个进程在空数据目录上首次建库时可能撞锁，需重试。
        last_exc: Exception | None = None
        for _ in range(30):
            conn = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA busy_timeout=15000")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA foreign_keys=ON")
                return conn
            except sqlite3.OperationalError as exc:
                conn.close()
                last_exc = exc
                time.sleep(0.2)
        assert last_exc is not None
        raise last_exc

    def _init_db(self) -> None:
        with self._db() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO meta(id, current_version, revision) VALUES (1, NULL, 0)"
            )

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _tx(self, conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def blob_path(self, digest: str) -> str:
        return os.path.join(self.blobs_dir, digest)

    # ---------- 启动恢复 ----------
    def recover(self, grace_seconds: float = DEFAULT_GRACE_SECONDS) -> None:
        """进程启动时调用：清理残留上传暂存目录并完成未结束的回收。

        用文件锁在多进程之间序列化；崩溃在“移动到 blob 之后、提交之前”留下的
        孤儿 blob 也会被清除（设宽限期，避免误删另一进程正在提交的目录）。
        """
        with open(self.lock_path, "w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                self._cleanup_staging(grace_seconds)
                self._complete_recycling()
                self._heal_missing_blobs()
                self._cleanup_orphan_blobs(grace_seconds)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _cleanup_staging(self, grace_seconds: float) -> None:
        now = time.time()
        for name in os.listdir(self.staging_dir):
            path = os.path.join(self.staging_dir, name)
            if not os.path.isdir(path):
                continue
            # 活跃进程刚创建的暂存目录不动（按 mtime 宽限）
            if now - os.path.getmtime(path) < grace_seconds:
                continue
            shutil.rmtree(path, ignore_errors=True)

    def _complete_recycling(self) -> None:
        """继续完成所有 recycling 版本的删除与解激活。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT digest FROM versions WHERE state = ?", (RECYCLING,)
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            digest = row["digest"]
            # 先彻底删除文件，再提交数据库状态
            shutil.rmtree(self.blob_path(digest), ignore_errors=True)
            self._finish_recycle(conn_digest=digest)

    def _finish_recycle(self, conn_digest: str) -> None:
        conn = self._connect()
        try:
            with self._tx(conn):
                row = conn.execute(
                    "SELECT state FROM versions WHERE digest = ?", (conn_digest,)
                ).fetchone()
                if row is None:
                    return
                if row["state"] != RECYCLING:
                    # 极端情况下被同内容重传复活，不应再删，直接返回
                    return
                conn.execute(
                    "UPDATE meta SET current_version = NULL WHERE current_version = ?",
                    (conn_digest,),
                )
                conn.execute("DELETE FROM files WHERE digest = ?", (conn_digest,))
                conn.execute("DELETE FROM versions WHERE digest = ?", (conn_digest,))
        finally:
            conn.close()

    def _heal_missing_blobs(self) -> None:
        """防御：数据库标记为可用/回收中，但 blob 目录已缺失 —— 移除悬空版本。"""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT digest FROM versions").fetchall()
            for row in rows:
                digest = row["digest"]
                if not os.path.isdir(self.blob_path(digest)):
                    with self._tx(conn):
                        conn.execute(
                            "UPDATE meta SET current_version = NULL WHERE current_version = ?",
                            (digest,),
                        )
                        conn.execute("DELETE FROM files WHERE digest = ?", (digest,))
                        conn.execute("DELETE FROM versions WHERE digest = ?", (digest,))
        finally:
            conn.close()

    def _cleanup_orphan_blobs(self, grace_seconds: float) -> None:
        """删除数据库中不存在的 blob 目录（上传崩溃残留），按 mtime 宽限。"""
        now = time.time()
        with self._db() as conn:
            known = {
                r["digest"]
                for r in conn.execute("SELECT digest FROM versions").fetchall()
            }
        for name in os.listdir(self.blobs_dir):
            if name in known:
                continue
            path = os.path.join(self.blobs_dir, name)
            if not os.path.isdir(path):
                continue
            if now - os.path.getmtime(path) < grace_seconds:
                continue
            shutil.rmtree(path, ignore_errors=True)

    # ---------- 上传 ----------
    def create_version(self, package: zipfile.ZipFile) -> tuple[str, bool, list[str]]:
        """解压校验并生成不可变版本。

        返回 ``(digest, created, paths)``。相同内容重复上传返回同一 digest。
        回收进行中的同内容版本冲突返回 409。
        """
        staging = os.path.join(self.staging_dir, uuid.uuid4().hex)
        # 顺带清掉超过宽限期的崩溃残留（并发清理幂等，忽略错误）
        try:
            self._cleanup_staging(grace_seconds=DEFAULT_GRACE_SECONDS)
        except OSError:
            pass
        os.makedirs(staging, exist_ok=False)
        try:
            try:
                digest, files = unpack_and_validate(package, staging)
            except PackageError:
                raise
            except (OSError, zipfile.BadZipFile) as exc:
                raise PackageError(f"发布包无法解析: {exc}") from exc

            if not _is_hex64(digest):  # 理论上不会发生
                raise PackageError("摘要格式非法")

            conn = self._connect()
            try:
                with self._tx(conn):
                    row = conn.execute(
                        "SELECT state FROM versions WHERE digest = ?", (digest,)
                    ).fetchone()
                    if row is not None:
                        if row["state"] == RECYCLING:
                            raise StoreError(409, "该版本正在回收，暂时无法上传相同内容")
                        paths = [
                            r["path"]
                            for r in conn.execute(
                                "SELECT path FROM files WHERE digest = ? ORDER BY path",
                                (digest,),
                            ).fetchall()
                        ]
                        return digest, False, paths

                    target = self.blob_path(digest)
                    if os.path.exists(target):
                        # 另一事务已建目录/崩溃残留：内容寻址，清空后放入本次内容
                        shutil.rmtree(target, ignore_errors=True)
                    # 先让内容就位，再提交版本行
                    os.rename(staging, target)

                    now = time.time()
                    conn.execute(
                        "INSERT INTO versions(digest, state, created_at) VALUES (?, ?, ?)",
                        (digest, AVAILABLE, now),
                    )
                    conn.executemany(
                        "INSERT INTO files(digest, path, size) VALUES (?, ?, ?)",
                        [(digest, p, s) for p, s in files],
                    )
                return digest, True, [p for p, _ in files]
            finally:
                conn.close()
        finally:
            # 提交成功时 staging 已改名，删除一个不存在的目录是 no-op
            shutil.rmtree(staging, ignore_errors=True)

    # ---------- 激活（乐观锁 CAS） ----------
    def activate(self, digest: str, expected_revision: int) -> int:
        """激活版本，必须携带当前修订号；成功后修订号 +1 并返回新修订号。"""
        if not _is_hex64(digest):
            raise StoreError(404, "版本不存在")
        conn = self._connect()
        try:
            with self._tx(conn):
                meta = conn.execute(
                    "SELECT current_version, revision FROM meta WHERE id = 1"
                ).fetchone()
                revision = meta["revision"]
                if expected_revision != revision:
                    raise StoreError(
                        409,
                        f"修订号冲突：当前为 {revision}，提交的是 {expected_revision}",
                        revision=revision,
                    )
                row = conn.execute(
                    "SELECT state FROM versions WHERE digest = ?", (digest,)
                ).fetchone()
                if row is None:
                    raise StoreError(404, "版本不存在或已回收")
                if row["state"] == RECYCLING:
                    raise StoreError(409, "该版本正在回收，无法激活")
                conn.execute(
                    "UPDATE meta SET current_version = ?, revision = ? WHERE id = 1",
                    (digest, revision + 1),
                )
                return revision + 1
        finally:
            conn.close()

    # ---------- 回收 ----------
    def start_recycle(self, digest: str) -> None:
        """开始回收非当前版本；回收当前版返回 409。"""
        if not _is_hex64(digest):
            raise StoreError(404, "版本不存在")
        conn = self._connect()
        try:
            with self._tx(conn):
                meta = conn.execute(
                    "SELECT current_version FROM meta WHERE id = 1"
                ).fetchone()
                if meta["current_version"] == digest:
                    raise StoreError(409, "不能回收当前激活版本")
                row = conn.execute(
                    "SELECT state FROM versions WHERE digest = ?", (digest,)
                ).fetchone()
                if row is None:
                    raise StoreError(404, "版本不存在或已回收")
                if row["state"] == RECYCLING:
                    raise StoreError(409, "该版本已在回收中")
                conn.execute(
                    "UPDATE versions SET state = ?, recycled_at = ? WHERE digest = ?",
                    (RECYCLING, time.time(), digest),
                )
        finally:
            conn.close()

        # 状态已持久化为 recycling：即使此后进程退出，重启恢复也会完成删除。
        # 测试钩子（RECYCLE_DELETE_DELAY，默认 0）：制造“回收已开始但未删除”窗口
        delay = float(os.environ.get("RECYCLE_DELETE_DELAY", "0"))
        if delay:
            time.sleep(delay)
        self._delete_blob_finalize(digest)

    def _delete_blob_finalize(self, digest: str) -> None:
        shutil.rmtree(self.blob_path(digest), ignore_errors=True)
        self._finish_recycle(digest)

    # ---------- 查询 ----------
    def current(self) -> dict | None:
        with self._db() as conn:
            meta = conn.execute(
                "SELECT current_version, revision FROM meta WHERE id = 1"
            ).fetchone()
        if meta["current_version"] is None:
            return None
        return {"version": meta["current_version"], "revision": meta["revision"]}

    def revision(self) -> int:
        with self._db() as conn:
            return conn.execute("SELECT revision FROM meta WHERE id = 1").fetchone()[
                "revision"
            ]

    def list_versions(self) -> list[dict]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT digest, state, created_at, recycled_at FROM versions ORDER BY created_at"
            ).fetchall()
        return [
            {
                "version": r["digest"],
                "state": r["state"],
                "created_at": r["created_at"],
                "recycled_at": r["recycled_at"],
            }
            for r in rows
        ]

    def get_version(self, digest: str) -> dict:
        if not _is_hex64(digest):
            raise StoreError(404, "版本不存在")
        with self._db() as conn:
            row = conn.execute(
                "SELECT digest, state, created_at FROM versions WHERE digest = ?",
                (digest,),
            ).fetchone()
            if row is None:
                raise StoreError(404, "版本不存在或已回收")
            files = [
                {"path": r["path"], "size": r["size"]}
                for r in conn.execute(
                    "SELECT path, size FROM files WHERE digest = ? ORDER BY path",
                    (digest,),
                ).fetchall()
            ]
        return {
            "version": row["digest"],
            "state": row["state"],
            "created_at": row["created_at"],
            "files": files,
        }

    # ---------- 按版本清单读取 ----------
    def lookup_file(self, digest: str, rel_path: str) -> sqlite3.Row:
        """确认版本存在且资源在该版本清单内。回收中 -> 410，未知 -> 404。"""
        if not _is_hex64(digest):
            raise StoreError(404, "未知版本或路径")
        with self._db() as conn:
            v = conn.execute(
                "SELECT state FROM versions WHERE digest = ?", (digest,)
            ).fetchone()
            if v is None:
                raise StoreError(404, "未知版本或路径")
            if v["state"] == RECYCLING:
                raise StoreError(410, "该版本正在回收")
            f = conn.execute(
                "SELECT size FROM files WHERE digest = ? AND path = ?",
                (digest, rel_path),
            ).fetchone()
        if f is None:
            raise StoreError(404, "未知版本或路径")
        return f

    def open_file(self, digest: str, rel_path: str):
        """打开版本清单中的文件，返回 (二进制文件对象, 大小)。

        先校验状态与清单，再打开文件；文件打开后即使回收开始（unlink 目录项），
        已建立的流式读取仍可读到完整内容（POSIX 语义）。
        """
        row = self.lookup_file(digest, rel_path)
        path = os.path.join(self.blob_path(digest), rel_path)
        try:
            fh = open(path, "rb")
        except FileNotFoundError:
            # 与回收竞争：按“回收中”对待
            state = self._state(digest)
            if state == RECYCLING:
                raise StoreError(410, "该版本正在回收")
            raise StoreError(404, "未知版本或路径")
        # 打开之后再确认一次状态：若回收已提交则放弃本次句柄
        if self._state(digest) == RECYCLING:
            fh.close()
            raise StoreError(410, "该版本正在回收")
        return fh, row["size"]

    def _state(self, digest: str) -> str | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT state FROM versions WHERE digest = ?", (digest,)
            ).fetchone()
        return row["state"] if row else None
