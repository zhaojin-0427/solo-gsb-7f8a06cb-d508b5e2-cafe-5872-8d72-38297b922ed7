"""激活：修订号乐观锁、冲突 409、允许重新激活旧版。"""

from __future__ import annotations

from tests.conftest import upload, valid_site


def _activate(client, digest, revision):
    return client.post(
        f"/versions/{digest}/activate", json={"revision": revision}
    )


def test_activate_first_revision(client):
    v = upload(client, valid_site(b"v1")).json()["version"]
    r = _activate(client, v, 0)
    assert r.status_code == 200
    assert r.json() == {"version": v, "revision": 1}
    assert client.get("/current").json() == {"version": v, "revision": 1}


def test_stale_revision_conflict(client):
    v1 = upload(client, valid_site(b"v1")).json()["version"]
    v2 = upload(client, valid_site(b"v2")).json()["version"]
    assert _activate(client, v1, 0).status_code == 200  # revision -> 1
    r = _activate(client, v2, 0)  # 旧修订号
    assert r.status_code == 409
    assert r.json()["error"]["revision"] == 1
    # 当前版本未变
    assert client.get("/current").json()["version"] == v1


def test_reactivate_old_version(client):
    v1 = upload(client, valid_site(b"v1")).json()["version"]
    v2 = upload(client, valid_site(b"v2")).json()["version"]
    _activate(client, v1, 0)
    assert _activate(client, v2, 1).status_code == 200
    # 重新激活旧版 v1，修订号继续递增
    r = _activate(client, v1, 2)
    assert r.status_code == 200
    assert r.json() == {"version": v1, "revision": 3}
    assert client.get("/current").json() == {"version": v1, "revision": 3}


def test_activate_unknown_version_404(client):
    r = _activate(client, "a" * 64, 0)
    assert r.status_code == 404


def test_activate_bad_revision_payload(client):
    v = upload(client, valid_site()).json()["version"]
    assert client.post(f"/versions/{v}/activate", json={}).status_code == 422
    assert (
        client.post(f"/versions/{v}/activate", json={"revision": "0"}).status_code
        == 422
    )
    assert (
        client.post(f"/versions/{v}/activate", json={"revision": True}).status_code
        == 422
    )
    # 非法 JSON 体同样返回 422
    r = client.post(
        f"/versions/{v}/activate",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert "error" in r.json()
