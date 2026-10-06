"""回收：当前版 409、开始后激活 409、新读 410、未知 404、再上传同内容可用。"""

from __future__ import annotations

import os
import time

from tests.conftest import make_zip, upload


def _activate(client, digest, revision=0):
    return client.post(
        f"/versions/{digest}/activate", json={"revision": revision}
    )


def test_recycle_current_version_409(client):
    v = upload(client, make_zip({"index.html": b"x"})).json()["version"]
    _activate(client, v)
    r = client.post(f"/versions/{v}/recycle")
    assert r.status_code == 409
    # 仍然可读
    assert client.get(f"/v/{v}/index.html").status_code == 200


def test_recycle_old_version(client):
    v1 = upload(client, make_zip({"index.html": b"v1"})).json()["version"]
    v2 = upload(client, make_zip({"index.html": b"v2"})).json()["version"]
    _activate(client, v1, 0)
    _activate(client, v2, 1)

    r = client.post(f"/versions/{v1}/recycle")
    assert r.status_code == 204
    # 回收完成后：版本未知，读取 404，激活 404
    assert client.get(f"/v/{v1}/index.html").status_code == 404
    assert _activate(client, v1, 2).status_code == 404
    assert client.get("/versions").json()["versions"][0]["version"] == v2


def test_recycling_window_blocks_activate_and_reads(client, monkeypatch, tmp_path):
    """通过测试钩子制造 recycling 窗口：激活 409、新读取 410。"""
    monkeypatch.setenv("RECYCLE_DELETE_DELAY", "0.5")
    import importlib

    import app.main as main

    importlib.reload(main)
    from fastapi.testclient import TestClient

    with TestClient(main.app) as c2:
        v1 = upload(c2, make_zip({"index.html": b"v1"})).json()["version"]
        v2 = upload(c2, make_zip({"index.html": b"v2"})).json()["version"]
        c2.post(f"/versions/{v1}/activate", json={"revision": 0})
        c2.post(f"/versions/{v2}/activate", json={"revision": 1})

        import threading

        errs = {}

        def do_recycle():
            errs["status"] = c2.post(f"/versions/{v1}/recycle").status_code

        t = threading.Thread(target=do_recycle)
        t.start()
        time.sleep(0.2)
        # 回收已开始但尚未完成
        assert c2.post(f"/versions/{v1}/activate", json={"revision": 2}).status_code == 409
        assert c2.get(f"/v/{v1}/index.html").status_code == 410
        t.join()
        assert errs["status"] == 204
        assert c2.get(f"/v/{v1}/index.html").status_code == 404


def test_reupload_after_recycle_is_new_version(client):
    v1 = upload(client, make_zip({"index.html": b"same"})).json()["version"]
    v2 = upload(client, make_zip({"index.html": b"other"})).json()["version"]
    _activate(client, v1, 0)
    _activate(client, v2, 1)
    assert client.post(f"/versions/{v1}/recycle").status_code == 204
    # 回收后重新上传相同内容：摘要相同，重新变为可用版本
    r = upload(client, make_zip({"index.html": b"same"}))
    assert r.status_code == 201
    assert r.json()["version"] == v1
    assert client.get(f"/v/{v1}/index.html").status_code == 200


def test_recycle_unknown_404(client):
    assert client.post(f"/versions/{'c' * 64}/recycle").status_code == 404


def test_recycled_blob_directory_removed(client, tmp_path):
    import glob

    v = upload(client, make_zip({"index.html": b"x"})).json()["version"]
    client.post(f"/versions/{v}/recycle")
    assert glob.glob(str(tmp_path / "data" / "blobs" / "*")) == []
