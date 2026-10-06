"""存储层：SQLite 元数据 + 本地文件目录的内容寻址存储。

目录布局（DATA_DIR）：
    app.db                 SQLite 数据库（WAL 模式，支持多进程并发）
    versions/<digest>/     每个版本的文件内容（按上传时的相对路径存放）
    tmp/                   上传解包的临时目录（rename 进 versions/ 前）

一致性设计：
  * 版本状态机：uploading -> ready -> reclaiming -> reclaimed（墓碑）。
    只有 ready 版本对外可见；回收完成后保留墓碑行，
    使"回收开始后新读取 410 / 激活 409"的语义持久成立。
  * 上传：先写库（uploading）-> 解包目录原子 rename 到位 -> 置 ready。
    任意时刻崩溃都不会留下"指向缺失文件的可见版本"。
  * 回收：先置 reclaiming（此后激活 409、新读取 410），再删文件、删行。
    已打开的文件描述符不受 unlink 影响，流式下载可完整结束。
  * 重启恢复：清理 uploading 残留、继续完成未完成的 reclaiming、
    清理过期临时目录与孤儿目录。
  * 多进程共用数据目录：所有状态迁移均为单条原子 SQL 或
    BEGIN IMMEDIATE 事务，靠 SQLite WAL + busy_timeout 串行化。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    id          TEXT PRIMARY KEY,
    state       TEXT NOT NULL CHECK (state IN ('uploading', 'ready',
                                             'reclaiming', 'reclaimed')),
    size_bytes  INTEGER NOT NULL,
    file_count  INTEGER NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS manifest (
    version_id  TEXT NOT NULL,
    path        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    PRIMARY KEY (version_id, path)
);
CREATE TABLE IF NOT EXISTS site_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    current_version TEXT,
    revision        INTEGER NOT NULL
);
"""

# 上传行超过该时长仍未 ready，视为崩溃残留，可被其他进程接管清理
STALE_UPLOAD_SECONDS = 60.0
# 非属主等待并发同内容上传完成的时长（须大于 STALE_UPLOAD_SECONDS，
# 以便对端崩溃后能接管清理并重试）
UPLOAD_WAIT_SECONDS = 90.0


class VersionNotFound(Exception):
    pass


class VersionGone(Exception):
    """版本正在回收或已被回收。"""


class RevisionConflict(Exception):
    def __init__(self, current_version: str | None, current_revision: int):
        super().__init__("revision conflict")
        self.current_version = current_version
        self.current_revision = current_revision


class VersionIsCurrent(Exception):
    pass


class UploadFailed(Exception):
    pass


class Storage:
    def __init__(self, data_dir: str, cleanup_grace_seconds: float = 60.0):
        self.data_dir = os.path.abspath(data_dir)
        self.versions_dir = os.path.join(self.data_dir, "versions")
        self.tmp_dir = os.path.join(self.data_dir, "tmp")
        self.db_path = os.path.join(self.data_dir, "app.db")
        self.cleanup_grace = cleanup_grace_seconds
        os.makedirs(self.versions_dir, exist_ok=True)
        os.makedirs(self.tmp_dir, exist_ok=True)
        self._init_db()
        self.recover()

    # ------------------------------------------------------------------ db

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextmanager
    def _tx(self):
        """写事务：BEGIN IMMEDIATE，多进程下安全串行化。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO site_state (id, current_version, revision)"
                " VALUES (1, NULL, 0)"
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------- recovery

    def recover(self) -> None:
        """启动恢复：不留下指向缺失文件的版本，未完成的回收继续完成。"""
        # 1) 中断的上传：行不可见，直接删行并清掉对应目录
        rows = self._query("SELECT id FROM versions WHERE state = 'uploading'")
        with self._tx() as conn:
            for r in rows:
                conn.execute("DELETE FROM manifest WHERE version_id = ?", (r["id"],))
                conn.execute("DELETE FROM versions WHERE id = ?", (r["id"],))
        for r in rows:
            shutil.rmtree(self._version_path(r["id"]), ignore_errors=True)

        # 2) 未完成的回收：继续完成（幂等，多进程同时执行也安全）
        rows = self._query("SELECT id FROM versions WHERE state = 'reclaiming'")
        for r in rows:
            self.finish_reclaim(r["id"])

        # 3) 过期临时目录（可能正被其他进程使用，故设宽限期）
        self._remove_stale_dirs(self.tmp_dir, self.cleanup_grace)

        # 4) 孤儿版本目录（无对应数据库行且足够旧）
        known = {r["id"] for r in self._query("SELECT id FROM versions")}
        for name in os.listdir(self.versions_dir):
            path = os.path.join(self.versions_dir, name)
            if name not in known and self._older_than_grace(path):
                shutil.rmtree(path, ignore_errors=True)

    def _older_than_grace(self, path: str) -> bool:
        try:
            return time.time() - os.path.getmtime(path) > self.cleanup_grace
        except OSError:
            return False

    @staticmethod
    def _remove_stale_dirs(directory: str, grace: float) -> None:
        now = time.time()
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            try:
                if now - os.path.getmtime(path) > grace:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass

    # -------------------------------------------------------------- helpers

    def _version_path(self, version_id: str) -> str:
        return os.path.join(self.versions_dir, version_id)

    def new_tmp_dir(self) -> str:
        return tempfile.mkdtemp(prefix="extract-", dir=self.tmp_dir)

    def _version_state(self, version_id: str) -> str | None:
        rows = self._query("SELECT state FROM versions WHERE id = ?", (version_id,))
        return rows[0]["state"] if rows else None

    # --------------------------------------------------------------- upload

    def store_version(
        self,
        version_id: str,
        manifest: list[tuple[str, int, str]],
        total_size: int,
        src_dir: str,
    ) -> tuple[str, bool]:
        """把解包好的目录登记为版本。返回 (version_id, 是否新建)。

        相同内容重复上传返回同一版本（created=False）。
        src_dir 的所有权移交给本函数（任何分支都会负责清理）。
        """
        deadline = time.time() + UPLOAD_WAIT_SECONDS
        while True:
            if time.time() > deadline:
                shutil.rmtree(src_dir, ignore_errors=True)
                raise UploadFailed("timed out waiting for concurrent upload")
            try:
                with self._tx() as conn:
                    conn.execute(
                        "INSERT INTO versions (id, state, size_bytes, file_count,"
                        " created_at) VALUES (?, 'uploading', ?, ?, ?)",
                        (version_id, total_size, len(manifest), time.time()),
                    )
                    conn.executemany(
                        "INSERT INTO manifest (version_id, path, size, sha256)"
                        " VALUES (?, ?, ?, ?)",
                        [(version_id, p, s, h) for p, s, h in manifest],
                    )
                owner = True
            except sqlite3.IntegrityError:
                owner = False

            if owner:
                return self._commit_owned_upload(version_id, src_dir)

            state = self._version_state(version_id)
            if state is None:
                continue  # 行被清理，重试成为属主
            if state == "ready":
                shutil.rmtree(src_dir, ignore_errors=True)
                return version_id, False
            if state in ("reclaiming", "reclaimed"):
                # 同内容曾被回收：幂等地完成回收、清除墓碑后重建
                self.finish_reclaim(version_id)
                with self._tx() as conn:
                    conn.execute(
                        "DELETE FROM versions WHERE id = ? AND state = 'reclaimed'",
                        (version_id,),
                    )
                continue

            # state == 'uploading'：另一进程正在上传相同内容
            row = self._query(
                "SELECT created_at FROM versions WHERE id = ?", (version_id,)
            )
            if row and time.time() - row[0]["created_at"] > STALE_UPLOAD_SECONDS:
                # 上传进程已崩溃：接管清理后重试
                with self._tx() as conn:
                    conn.execute(
                        "DELETE FROM manifest WHERE version_id = ?", (version_id,)
                    )
                    conn.execute(
                        "DELETE FROM versions WHERE id = ? AND state = 'uploading'",
                        (version_id,),
                    )
                shutil.rmtree(self._version_path(version_id), ignore_errors=True)
                continue
            time.sleep(0.1)

    def _commit_owned_upload(self, version_id: str, src_dir: str) -> tuple[str, bool]:
        dest = self._version_path(version_id)
        try:
            if os.path.exists(dest):
                # 持有数据库行 => 该目录只可能是崩溃残留
                shutil.rmtree(dest)
            os.rename(src_dir, dest)  # 原子发布：文件一次性全部就位
        except BaseException:
            with self._tx() as conn:
                conn.execute("DELETE FROM manifest WHERE version_id = ?", (version_id,))
                conn.execute("DELETE FROM versions WHERE id = ?", (version_id,))
            shutil.rmtree(src_dir, ignore_errors=True)
            raise
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE versions SET state = 'ready'"
                " WHERE id = ? AND state = 'uploading'",
                (version_id,),
            )
            if cur.rowcount == 0:
                raise UploadFailed("version row disappeared during upload")
        return version_id, True

    # ----------------------------------------------------------- activation

    def current(self) -> dict:
        rows = self._query(
            "SELECT current_version, revision FROM site_state WHERE id = 1"
        )
        return {
            "version": rows[0]["current_version"],
            "revision": rows[0]["revision"],
        }

    def activate(self, version_id: str, expected_revision: int) -> dict:
        """携带当前修订号的 CAS 激活；允许重新激活旧版本。"""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT state FROM versions WHERE id = ?", (version_id,)
            ).fetchone()
            if row is None or row["state"] == "uploading":
                raise VersionNotFound(version_id)
            if row["state"] in ("reclaiming", "reclaimed"):
                raise VersionGone(version_id)
            cur = conn.execute(
                "SELECT current_version, revision FROM site_state WHERE id = 1"
            ).fetchone()
            if cur["revision"] != expected_revision:
                raise RevisionConflict(cur["current_version"], cur["revision"])
            conn.execute(
                "UPDATE site_state SET current_version = ?, revision = revision + 1"
                " WHERE id = 1 AND revision = ?",
                (version_id, expected_revision),
            )
        return {"version": version_id, "revision": expected_revision + 1}

    # -------------------------------------------------------------- reclaim

    def begin_reclaim(self, version_id: str) -> None:
        """把版本置为 reclaiming：此后激活 409、新读取 410。"""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT state FROM versions WHERE id = ?", (version_id,)
            ).fetchone()
            if row is None or row["state"] == "uploading":
                raise VersionNotFound(version_id)
            cur = conn.execute(
                "SELECT current_version FROM site_state WHERE id = 1"
            ).fetchone()
            if cur["current_version"] == version_id:
                raise VersionIsCurrent(version_id)
            if row["state"] in ("reclaiming", "reclaimed"):
                return  # 幂等：回收已在进行或已完成
            conn.execute(
                "UPDATE versions SET state = 'reclaiming'"
                " WHERE id = ? AND state = 'ready'",
                (version_id,),
            )

    def finish_reclaim(self, version_id: str) -> None:
        """删除文件与清单，留下墓碑行。幂等；崩溃后由 recover() 重新调用。"""
        shutil.rmtree(self._version_path(version_id), ignore_errors=True)
        with self._tx() as conn:
            conn.execute("DELETE FROM manifest WHERE version_id = ?", (version_id,))
            conn.execute(
                "UPDATE versions SET state = 'reclaimed'"
                " WHERE id = ? AND state IN ('reclaiming', 'reclaimed')",
                (version_id,),
            )

    # --------------------------------------------------------------- queries

    def list_versions(self) -> list[dict]:
        rows = self._query(
            "SELECT id, size_bytes, file_count, created_at FROM versions"
            " WHERE state = 'ready' ORDER BY created_at DESC"
        )
        return [
            {
                "id": r["id"],
                "size_bytes": r["size_bytes"],
                "file_count": r["file_count"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def get_version(self, version_id: str) -> dict:
        rows = self._query(
            "SELECT state, id, size_bytes, file_count, created_at FROM versions"
            " WHERE id = ?",
            (version_id,),
        )
        if not rows or rows[0]["state"] == "uploading":
            raise VersionNotFound(version_id)
        if rows[0]["state"] in ("reclaiming", "reclaimed"):
            raise VersionGone(version_id)
        row = rows[0]
        files = self._query(
            "SELECT path, size, sha256 FROM manifest WHERE version_id = ? ORDER BY path",
            (version_id,),
        )
        return {
            "id": row["id"],
            "size_bytes": row["size_bytes"],
            "file_count": row["file_count"],
            "created_at": row["created_at"],
            "files": [
                {"path": f["path"], "size": f["size"], "sha256": f["sha256"]}
                for f in files
            ],
        }

    # --------------------------------------------------------------- serving

    def open_file(self, version_id: str, path: str):
        """按版本清单打开文件。返回 (文件对象, size, sha256)。

        在回收开始后才到达的读取得到 410；回收前已打开的文件
        描述符不受 unlink 影响，流式下载可完整结束。
        """
        state = self._version_state(version_id)
        if state is None or state == "uploading":
            raise VersionNotFound(version_id)
        if state in ("reclaiming", "reclaimed"):
            raise VersionGone(version_id)
        if any(seg in ("", ".", "..") for seg in path.split("/")):
            raise VersionNotFound(path)  # 清单内不可能存在此类路径
        rows = self._query(
            "SELECT size, sha256 FROM manifest WHERE version_id = ? AND path = ?",
            (version_id, path),
        )
        if not rows:
            raise VersionNotFound(path)
        fpath = os.path.join(self._version_path(version_id), *path.split("/"))
        try:
            fd = open(fpath, "rb")
        except FileNotFoundError:
            raise VersionGone(version_id)  # 与回收并发：文件刚被删除
        return fd, rows[0]["size"], rows[0]["sha256"]
