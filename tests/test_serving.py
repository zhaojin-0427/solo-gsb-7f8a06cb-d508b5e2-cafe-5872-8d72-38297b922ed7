"""站点入口与按版本读取：404、跳转、版本隔离、未知路径 404。"""

from __future__ import annotations

from tests.conftest import make_zip, upload


def _activate(client, digest, revision=0):
    return client.post(
        f"/versions/{digest}/activate", json={"revision": revision}
    )


def test_root_404_when_inactive(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 404


def test_head_root_redirect_and_inactive(client):
    r = client.head("/", follow_redirects=False)
    assert r.status_code == 404
    v = upload(client, make_zip({"index.html": b"x"})).json()["version"]
    _activate(client, v)
    r = client.head("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == f"/v/{v}/index.html"


def test_root_redirects_to_versioned_index(client):
    v = upload(
        client,
        make_zip(
            {
                "index.html": b'<link rel="stylesheet" href="css/app.css">',
                "css/app.css": b"h1{color:red}",
            }
        ),
    ).json()["version"]
    _activate(client, v)

    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == f"/v/{v}/index.html"


def test_version_pinned_resources(client):
    v = upload(
        client,
        make_zip(
            {
                "index.html": b"V1",
                "css/app.css": b"v1-style",
            }
        ),
    ).json()["version"]
    _activate(client, v)
    # 版本化读取
    r = client.get(f"/v/{v}/index.html")
    assert r.status_code == 200
    assert r.content == b"V1"
    r2 = client.get(f"/v/{v}/css/app.css")
    assert r2.status_code == 200
    assert r2.content == b"v1-style"


def test_switch_versions_no_mixing(client):
    v1 = upload(client, make_zip({"index.html": b"V1", "x.txt": b"x1"})).json()[
        "version"
    ]
    v2 = upload(
        client, make_zip({"index.html": b"V2", "y.txt": b"y2"})
    ).json()["version"]

    _activate(client, v1, 0)
    assert client.get("/", follow_redirects=False).headers["location"].endswith(
        f"/v/{v1}/index.html"
    )
    # 旧版本仍可直接按版本号读取
    assert client.get(f"/v/{v1}/x.txt").content == b"x1"
    assert client.get(f"/v/{v1}/y.txt").status_code == 404

    _activate(client, v2, 1)
    assert client.get("/", follow_redirects=False).headers["location"].endswith(
        f"/v/{v2}/index.html"
    )
    # 切换后两个版本各自隔离，不存在混用
    assert client.get(f"/v/{v2}/index.html").content == b"V2"
    assert client.get(f"/v/{v2}/x.txt").status_code == 404
    assert client.get(f"/v/{v1}/y.txt").status_code == 404
    assert client.get(f"/v/{v1}/index.html").content == b"V1"


def test_unknown_version_404(client):
    assert client.get(f"/v/{'b' * 64}/index.html").status_code == 404


def test_unknown_path_404(client):
    v = upload(client, make_zip({"index.html": b"x"})).json()["version"]
    assert client.get(f"/v/{v}/nope.js").status_code == 404
    # 路径穿越不成立：清单外一律 404
    assert client.get(f"/v/{v}/..%2f..%2fetc").status_code == 404


def test_version_root_redirect(client):
    v = upload(client, make_zip({"index.html": b"x"})).json()["version"]
    r = client.get(f"/v/{v}", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == f"/v/{v}/index.html"
    r2 = client.get(f"/v/{v}/", follow_redirects=False)
    assert r2.status_code == 307


def test_head_request(client):
    v = upload(client, make_zip({"index.html": b"abc"})).json()["version"]
    r = client.head(f"/v/{v}/index.html")
    assert r.status_code == 200
    assert r.headers["content-length"] == "3"
    assert r.content == b""
