"""测试公共工具：临时数据目录、ASGI 客户端、ZIP 构造。"""

from __future__ import annotations

import io
import os
import stat
import zipfile

import pytest

# 让测试在无虚拟环境的机器上也能找到用户态安装的依赖
_EXTRA = "/workspace/.pylib/lib/python3.11/site-packages"
if os.path.isdir(_EXTRA) and _EXTRA not in os.environ.get("PYTHONPATH", ""):
    import sys

    sys.path.insert(0, _EXTRA)

os.environ.setdefault("RECOVERY_GRACE_SECONDS", "0")

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    # 延迟导入，确保读到环境变量
    import importlib

    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


def make_zip(entries: dict, *, mode: str = "w") -> bytes:
    """entries: {name: bytes}；以 '/' 结尾的名字作为目录条目。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode, zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def add_unix_entry(data: bytes, name: str, payload: bytes, unix_type: int) -> bytes:
    """在现有 ZIP 上追加一个指定 unix 类型的条目（如符号链接）。"""
    buf = io.BytesIO(data)
    with zipfile.ZipFile(buf, "a", zipfile.ZIP_STORED) as zf:
        info = zipfile.ZipInfo(name)
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, payload)
    return buf.getvalue()


def valid_site(body: bytes = b"<h1>hello</h1>") -> bytes:
    return make_zip({"index.html": body, "css/app.css": b"h1{color:red}"})


def upload(client, data: bytes, filename: str = "site.zip"):
    return client.post(
        "/versions",
        files={"file": (filename, data, "application/zip")},
    )
