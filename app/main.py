"""静态站点发布 HTTP API。

启动时对数据目录执行崩溃恢复（完成未结束回收、清理残留暂存）。
两个进程共用同一数据目录时，写事务由 SQLite BEGIN IMMEDIATE 序列化，
启动恢复由 fcntl 文件锁序列化。
"""

from __future__ import annotations

import io
import mimetypes
import os
import zipfile
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, StrictInt

from .package import PackageError
from .store import Store, StoreError

# 上传的压缩包大小上限（解压上限 16 MiB 在包解析时强制）
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(64 * 1024 * 1024)))
# 启动恢复对暂存/孤儿目录的宽限秒数（多进程错峰启动时避免误删活跃目录）
RECOVERY_GRACE_SECONDS = float(os.environ.get("RECOVERY_GRACE_SECONDS", "60"))

# slim 镜像里 mime 数据库可能不全，显式补齐站点常见类型
_EXTRA_MIMETYPES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ico": "image/x-icon",
}
for _ext, _type in _EXTRA_MIMETYPES.items():
    mimetypes.add_type(_type, _ext)

DATA_DIR = os.environ.get("DATA_DIR", "/data" if os.path.isdir("/data") else "./data")

store = Store(DATA_DIR)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动恢复可能执行删除，放到线程里避免阻塞事件循环
    await anyio.to_thread.run_sync(store.recover, RECOVERY_GRACE_SECONDS)
    yield


app = FastAPI(title="静态站点发布 API", version="1.0.0", lifespan=lifespan)


def _error(code: int, detail: str, revision: int | None = None) -> JSONResponse:
    body: dict = {"error": {"code": code, "detail": detail}}
    if revision is not None:
        body["error"]["revision"] = revision
    return JSONResponse(status_code=code, content=body)


@app.exception_handler(StoreError)
async def store_error_handler(request: Request, exc: StoreError) -> JSONResponse:
    return _error(exc.code, exc.detail, exc.revision)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _error(422, "请求参数不合法: " + str(exc.errors()))


class ActivateBody(BaseModel):
    # StrictInt 拒绝 "0"、true/false、1.0 等
    revision: StrictInt


# ---------- 运维接口 ----------
@app.get("/healthz")
@app.head("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/versions")
async def upload_version(file: UploadFile):
    """上传 ZIP 生成不可变版本。相同内容返回同一版本。"""
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return _error(413, f"上传包超过 {MAX_UPLOAD_BYTES} 字节上限")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        # 解压与数据库写入放入线程池，避免阻塞事件循环
        digest, created, paths = await anyio.to_thread.run_sync(
            store.create_version, zf
        )
    except zipfile.BadZipFile:
        return _error(422, "不是合法的 ZIP 文件")
    except PackageError as exc:
        return _error(422, str(exc))
    return JSONResponse(
        status_code=201 if created else 200,
        content={"version": digest, "created": created, "files": paths},
    )


@app.get("/versions")
@app.head("/versions")
def versions() -> dict:
    return {"versions": store.list_versions()}


@app.get("/versions/{digest}")
@app.head("/versions/{digest}")
def version_detail(digest: str) -> dict:
    return store.get_version(digest)


@app.post("/versions/{digest}/activate")
def activate(digest: str, body: ActivateBody) -> JSONResponse:
    """激活版本；请求体携带当前修订号 {"revision": N}，冲突返回 409。"""
    new_revision = store.activate(digest, body.revision)
    return JSONResponse(content={"version": digest, "revision": new_revision})


@app.post("/versions/{digest}/recycle")
def recycle(digest: str) -> Response:
    """回收非当前版本；当前版本返回 409，回收开始后激活返回 409、读取返回 410。"""
    store.start_recycle(digest)
    return Response(status_code=204)


@app.get("/current")
@app.head("/current")
def current() -> JSONResponse:
    cur = store.current()
    if cur is None:
        return JSONResponse(
            content={"version": None, "revision": store.revision()}
        )
    return JSONResponse(content=cur)


# ---------- 站点入口与按版本读取 ----------
def _content_type(rel_path: str) -> str:
    ctype, _ = mimetypes.guess_type(rel_path)
    return ctype or "application/octet-stream"


def _serve_version_file(digest: str, rel_path: str, head: bool) -> Response:
    fh, size = store.open_file(digest, rel_path)
    headers = {"Content-Length": str(size)}
    if head:
        fh.close()
        return Response(
            status_code=200, headers=headers, media_type=_content_type(rel_path)
        )

    def iterfile():
        try:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            fh.close()

    return StreamingResponse(
        iterfile(),
        status_code=200,
        headers=headers,
        media_type=_content_type(rel_path),
    )


@app.get("/v/{digest}")
@app.head("/v/{digest}")
def version_root_noslash(digest: str) -> Response:
    return RedirectResponse(url=f"/v/{digest}/index.html", status_code=307)


@app.get("/v/{digest}/{rel_path:path}")
@app.head("/v/{digest}/{rel_path:path}")
def version_file(digest: str, rel_path: str, request: Request) -> Response:
    # /v/<digest> 或 /v/<digest>/：跳转到该版本的 index.html，
    # 页面内相对资源（css/app.js 等）由此始终落在 /v/<digest>/ 前缀下
    if rel_path in ("", "/"):
        return RedirectResponse(url=f"/v/{digest}/index.html", status_code=307)
    return _serve_version_file(digest, rel_path, head=request.method == "HEAD")


@app.get("/")
@app.head("/")
def site_root() -> Response:
    """未激活 -> 404；激活后 307 跳转到带版本号的 index.html。"""
    cur = store.current()
    if cur is None:
        return _error(404, "站点尚未激活任何版本")
    return RedirectResponse(
        url=f"/v/{cur['version']}/index.html", status_code=307
    )
