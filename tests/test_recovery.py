"""崩溃恢复：上传/回收中途进程退出，重启后不得留下悬空版本，回收必须完成。"""

from __future__ import annotations

import io
import os
import sqlite3
import zipfile

import pytest

from tests.conftest import make_zip


def _store(tmp_path):
    from app.store import Store

    return Store(str(tmp_path / "data"))


def test_interrupted_upload_staging_cleaned(tmp_path):
    # 第一次启动，上传一个版本建立基础结构
    from app.store import AVAILABLE, Store

    data_dir = tmp_path / "data"
    store = Store(str(data_dir))
    digest, _, _ = store.create_version(
        zipfile.ZipFile(io.BytesIO(make_zip({"index.html": b"base"})))
    )

    # 模拟“解压到 staging 后、提交前进程退出”：留一个暂存目录
    stale = data_dir / "staging" / "deadbeef"
    stale.mkdir(parents=True)
    (stale / "index.html").write_bytes(b"ghost")
    old_mtime = os.stat(stale).st_mtime - 120
    os.utime(stale, (old_mtime, old_mtime))

    # 重启恢复：暂存目录被清，版本表不出现幽灵版本
    Store(str(data_dir)).recover(grace_seconds=60)
    assert not stale.exists()
    conn = sqlite3.connect(str(data_dir / "db.sqlite3"))
    rows = conn.execute("SELECT digest FROM versions").fetchall()
    conn.close()
    assert rows == [(digest,)]


def test_interrupted_recycle_completed_on_restart(tmp_path):
    from app.store import RECYCLING, Store

    data_dir = tmp_path / "data"
    store = Store(str(data_dir))
    digest, _, _ = store.create_version(
        zipfile.ZipFile(
            io.BytesIO(make_zip({"index.html": b"x", "deep/a.txt": b"a"}))
        )
    )
    blob = data_dir / "blobs" / digest

    # 模拟回收进行到“状态已置 recycling、文件尚未删完、进程退出”
    conn = sqlite3.connect(str(data_dir / "db.sqlite3"))
    conn.execute(
        "UPDATE versions SET state = ? WHERE digest = ?", (RECYCLING, digest)
    )
    conn.commit()
    conn.close()
    # 甚至先删掉部分文件，模拟半途中断
    os.remove(blob / "deep" / "a.txt")
    assert blob.exists()

    # 重启：未完成回收必须继续完成
    Store(str(data_dir)).recover(grace_seconds=0)
    assert not blob.exists()
    conn = sqlite3.connect(str(data_dir / "db.sqlite3"))
    assert conn.execute(
        "SELECT COUNT(*) FROM versions WHERE digest = ?", (digest,)
    ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    conn.close()


def test_current_version_untouched_by_recovery(tmp_path):
    from app.store import Store

    data_dir = tmp_path / "data"
    store = Store(str(data_dir))
    digest, _, _ = store.create_version(
        zipfile.ZipFile(io.BytesIO(make_zip({"index.html": b"x"})))
    )
    store.activate(digest, 0)

    Store(str(data_dir)).recover(grace_seconds=0)
    cur = Store(str(data_dir)).current()
    assert cur == {"version": digest, "revision": 1}


def test_orphan_blob_after_rename_crash_removed(tmp_path):
    """移动到 blobs 后、提交事务前崩溃：孤儿 blob 由重启恢复清理。"""
    from app.store import Store

    data_dir = tmp_path / "data"
    Store(str(data_dir))  # 初始化
    orphan_dir = data_dir / "blobs" / ("a" * 64)
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "index.html").write_bytes(b"orphan")
    old = os.stat(orphan_dir).st_mtime - 120
    os.utime(orphan_dir, (old, old))

    Store(str(data_dir)).recover(grace_seconds=60)
    assert not orphan_dir.exists()
