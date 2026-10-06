"""上传校验：所有非法包返回 422，且不产生可见版本。"""

from __future__ import annotations

from tests.conftest import add_unix_entry, make_zip, upload, valid_site


def test_valid_upload_201(client):
    r = upload(client, valid_site())
    assert r.status_code == 201, r.text
    body = r.json()
    assert len(body["version"]) == 64
    assert body["created"] is True
    assert body["files"] == ["css/app.css", "index.html"]


def test_duplicate_content_same_version(client):
    r1 = upload(client, valid_site())
    r2 = upload(client, valid_site())
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r1.json()["version"] == r2.json()["version"]
    assert r2.json()["created"] is False
    assert client.get("/versions").json()["versions"].__len__() == 1


def test_different_content_different_version(client):
    v1 = upload(client, valid_site(b"a")).json()["version"]
    v2 = upload(client, valid_site(b"b")).json()["version"]
    assert v1 != v2


def test_empty_zip_rejected(client):
    r = upload(client, make_zip({}))
    assert r.status_code == 422
    assert client.get("/versions").json()["versions"] == []


def test_missing_index_rejected(client):
    r = upload(client, make_zip({"a.html": b"x"}))
    assert r.status_code == 422
    assert "index.html" in r.json()["error"]["detail"]
    assert client.get("/versions").json()["versions"] == []


def test_index_in_subdir_rejected(client):
    r = upload(client, make_zip({"dir/index.html": b"x"}))
    assert r.status_code == 422


def test_parent_segment_rejected(client):
    r = upload(client, make_zip({"../evil": b"x", "index.html": b"x"}))
    assert r.status_code == 422


def test_dot_segment_rejected(client):
    r = upload(client, make_zip({"./index.html": b"x"}))
    assert r.status_code == 422


def test_absolute_path_rejected(client):
    r = upload(client, make_zip({"/index.html": b"x"}))
    assert r.status_code == 422


def test_empty_segment_rejected(client):
    r = upload(client, make_zip({"a//b.js": b"x", "index.html": b"x"}))
    assert r.status_code == 422


def test_backslash_rejected(client):
    r = upload(client, make_zip({"a\\b.js": b"x", "index.html": b"x"}))
    assert r.status_code == 422


def test_duplicate_name_rejected(client):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("index.html", b"x")
        zf.writestr("index.html", b"y")
    r = upload(client, buf.getvalue())
    assert r.status_code == 422
    assert client.get("/versions").json()["versions"] == []


def test_symlink_rejected(client):
    data = add_unix_entry(valid_site(), "evil", b"/etc/passwd", 0o120000)
    r = upload(client, data)
    assert r.status_code == 422
    assert "符号链接" in r.json()["error"]["detail"]
    assert client.get("/versions").json()["versions"] == []


def test_file_dir_name_collision(client):
    r = upload(client, make_zip({"index.html": b"x", "index.html/x": b"y"}))
    assert r.status_code == 422


def test_total_size_limit_16m(client):
    big = b"a" * (16 * 1024 * 1024 + 1)
    r = upload(client, make_zip({"index.html": b"x", "big.bin": big}))
    assert r.status_code == 422
    assert "16 MiB" in r.json()["error"]["detail"]
    assert client.get("/versions").json()["versions"] == []


def test_exactly_at_limit_ok(client):
    # index 1 字节 + 填充到恰好 16 MiB
    pad = b"a" * (16 * 1024 * 1024 - 1)
    r = upload(client, make_zip({"index.html": b"x", "pad.bin": pad}))
    assert r.status_code == 201, r.text


def test_explicit_dir_entries_ok_and_digest_stable(client):
    import io
    import zipfile

    def zip_with_dirs(with_dir: bool) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            if with_dir:
                z.writestr(zipfile.ZipInfo("assets/"), b"")
            z.writestr("assets/a.js", b"1")
            z.writestr("index.html", b"x")
        return buf.getvalue()

    r1 = upload(client, zip_with_dirs(True))
    r2 = upload(client, zip_with_dirs(False))
    assert r1.status_code == 201
    assert r2.status_code == 200  # 摘要不依赖空目录条目
    assert r1.json()["version"] == r2.json()["version"]


def test_fifo_entry_rejected(client):
    import io
    import stat
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("index.html", b"x")
        info = zipfile.ZipInfo("fifo")
        info.create_system = 3
        info.external_attr = (stat.S_IFIFO | 0o644) << 16
        z.writestr(info, b"")
    assert upload(client, buf.getvalue()).status_code == 422


def test_not_a_zip(client):
    r = upload(client, b"this is not zip at all" * 10)
    assert r.status_code == 422
