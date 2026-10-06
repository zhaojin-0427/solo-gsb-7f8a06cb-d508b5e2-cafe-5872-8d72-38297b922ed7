"""静态站点发布 HTTP API。

接口一览：
    POST   /api/versions            上传 ZIP，生成不可变版本（内容寻址）
    GET    /api/versions            版本列表
    GET    /api/versions/{id}       版本详情（含清单）
    DELETE /api/versions/{id}       回收版本（当前版本 409）
    GET    /api/site                查询当前版本与修订号
    PUT    /api/site/active         携带修订号激活版本（冲突 409）
    GET    /                        未激活 404；已激活 302 到带版本号的 index.html
    GET    /site/{id}/{path}        按版本清单读取资源（未知 404，已回收 410）
"""

from __future__ import annotations

import mimetypes
import os
import shutil
import zipfile
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from .storage import (
    RevisionConflict,
    Storage,
    UploadFailed,
    VersionGone,
    VersionIsCurrent,
    VersionNotFound,
)
from .zipping import ZipRejected, extract_and_digest, validate_entries

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
CLEANUP_GRACE_SECONDS = float(os.environ.get("CLEANUP_GRACE_SECONDS", "60"))

storage = Storage(DATA_DIR, cleanup_grace_seconds=CLEANUP_GRACE_SECONDS)
app = FastAPI(title="Static Site Publisher", version="1.0.0")

_EXTRA_MIME = {
    ".html": "text/html", ".htm": "text/html", ".css": "text/css",
    ".js": "text/javascript", ".mjs": "text/javascript", ".json": "application/json",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".ico": "image/x-icon", ".txt": "text/plain", ".xml": "application/xml",
    ".pdf": "application/pdf", ".wasm": "application/wasm", ".map": "application/json",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
}


def _guess_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in _EXTRA_MIME:
        return _EXTRA_MIME[ext]
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# --------------------------------------------------------------------- API


@app.post("/api/versions")
def upload_version(file: UploadFile = File(...)):
    """上传 ZIP 生成不可变版本；相同内容重复上传返回同一版本。"""
    # 合法包解压后 ≤16 MiB，压缩后不可能超过该上限太多，超限直接拒绝
    file.file.seek(0, os.SEEK_END)
    compressed_size = file.file.tell()
    file.file.seek(0)
    if compressed_size > 64 * 1024 * 1024:
        raise _unprocessable("archive too large")
    tmp_dir = storage.new_tmp_dir()
    try:
        try:
            zf = zipfile.ZipFile(file.file)
        except zipfile.BadZipFile:
            raise _unprocessable("not a valid zip archive")
        with zf:
            try:
                files = validate_entries(zf)
                digest, manifest, total = extract_and_digest(zf, files, tmp_dir)
            except ZipRejected as exc:
                raise _unprocessable(exc.reason)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    try:
        version_id, created = storage.store_version(digest, manifest, total, tmp_dir)
    except VersionGone:
        raise HTTPException(409, "version is being reclaimed")
    except UploadFailed as exc:
        raise HTTPException(500, str(exc))
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)  # 兜底：任何分支都不留垃圾
        raise
    body = {
        "version": version_id,
        "size_bytes": total,
        "file_count": len(manifest),
        "created": created,
    }
    return JSONResponse(body, status_code=201 if created else 200)


@app.get("/api/versions")
def list_versions():
    cur = storage.current()
    versions = []
    for v in storage.list_versions():
        versions.append(
            {
                "id": v["id"],
                "size_bytes": v["size_bytes"],
                "file_count": v["file_count"],
                "created_at": _iso(v["created_at"]),
                "active": v["id"] == cur["version"],
            }
        )
    return {"current": cur, "versions": versions}


@app.get("/api/versions/{version_id}")
def get_version(version_id: str):
    try:
        v = storage.get_version(version_id)
    except VersionNotFound:
        raise HTTPException(404, "version not found")
    except VersionGone:
        raise HTTPException(410, "version has been reclaimed")
    v["created_at"] = _iso(v["created_at"])
    v["active"] = storage.current()["version"] == version_id
    return v


@app.delete("/api/versions/{version_id}", status_code=202)
def reclaim_version(version_id: str, background_tasks: BackgroundTasks):
    """回收非当前版本：回收开始后激活 409、新读取 410。"""
    try:
        storage.begin_reclaim(version_id)
    except VersionNotFound:
        raise HTTPException(404, "version not found")
    except VersionIsCurrent:
        raise HTTPException(409, "cannot reclaim the currently active version")
    background_tasks.add_task(storage.finish_reclaim, version_id)
    return {"version": version_id, "status": "reclaiming"}


@app.get("/api/site")
def get_site():
    """查询当前激活版本与修订号。"""
    return storage.current()


class ActivateRequest(BaseModel):
    version: str
    revision: int = Field(..., ge=0)


@app.put("/api/site/active")
def activate_version(body: ActivateRequest):
    """携带当前修订号激活版本（CAS）；允许重新激活旧版本。"""
    try:
        return storage.activate(body.version, body.revision)
    except VersionNotFound:
        raise HTTPException(404, "version not found")
    except VersionGone:
        raise HTTPException(409, "version is being reclaimed")
    except RevisionConflict as exc:
        return JSONResponse(
            {
                "detail": "revision conflict",
                "current_version": exc.current_version,
                "current_revision": exc.current_revision,
            },
            status_code=409,
        )


# -------------------------------------------------------------------- site


@app.get("/", include_in_schema=False)
def site_root():
    cur = storage.current()
    if not cur["version"]:
        raise HTTPException(404, "no active version")
    return RedirectResponse(
        f"/site/{cur['version']}/index.html", status_code=302
    )


@app.get("/site/{version_id}", include_in_schema=False)
def site_version_root(version_id: str):
    # 重定向到带版本号的 index.html，保证页面相对资源解析到本版本目录下
    return RedirectResponse(f"/site/{version_id}/index.html", status_code=302)


@app.get("/site/{version_id}/{path:path}", include_in_schema=False)
def site_file(version_id: str, path: str):
    if path == "":
        return RedirectResponse(f"/site/{version_id}/index.html", status_code=302)
    return _serve(version_id, path)


def _serve(version_id: str, path: str):
    try:
        fd, size, sha256 = storage.open_file(version_id, path)
    except VersionNotFound:
        raise HTTPException(404, "resource not found")
    except VersionGone:
        raise HTTPException(410, "version has been reclaimed")

    def stream():
        try:
            while True:
                chunk = fd.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            fd.close()

    return StreamingResponse(
        stream(),
        media_type=_guess_type(path),
        headers={
            "Content-Length": str(size),
            "ETag": f'"{sha256}"',
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)
