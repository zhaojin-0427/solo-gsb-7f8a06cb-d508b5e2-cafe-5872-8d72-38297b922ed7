"""ZIP 包校验、摘要计算与安全解包。

规则（违反即抛 ZipRejected，对应 HTTP 422）：
  * 条目只能是普通文件或目录（拒绝符号链接、设备节点等特殊条目）；
  * 路径以 '/' 分隔的相对路径，拒绝空段、'.'、'..'、反斜杠、绝对路径；
  * 拒绝重复条目名、文件/目录路径冲突；
  * 解压后总量不得超过 16 MiB；
  * 根目录必须包含 index.html。

版本摘要：按字典序排序后的文件路径 + 文件长度 + 文件字节做 SHA-256，
相同内容必然得到相同版本号（内容寻址）。
"""

from __future__ import annotations

import hashlib
import os
import stat
import struct
import zipfile

MAX_TOTAL_BYTES = 16 * 1024 * 1024  # 16 MiB
_CHUNK = 64 * 1024


class ZipRejected(Exception):
    """ZIP 包不符合发布规范。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _validate_name(raw: str) -> tuple[str, bool]:
    """校验条目名，返回 (规范化路径, 是否目录)。"""
    if not raw:
        raise ZipRejected("empty entry name")
    if "\x00" in raw:
        raise ZipRejected(f"NUL byte in entry name: {raw!r}")
    if "\\" in raw:
        raise ZipRejected(f"backslash in entry name: {raw!r}")
    if raw.startswith("/"):
        raise ZipRejected(f"absolute path not allowed: {raw!r}")
    is_dir = raw.endswith("/")
    parts = raw.split("/")
    if is_dir:
        parts = parts[:-1]  # 末尾 '/' 表示目录条目
    for seg in parts:
        if seg == "":
            raise ZipRejected(f"empty path segment in entry name: {raw!r}")
        if seg in (".", ".."):
            raise ZipRejected(f"illegal path segment {seg!r} in entry name: {raw!r}")
    if not parts:
        raise ZipRejected(f"entry names filesystem root: {raw!r}")
    return "/".join(parts), is_dir


def _entry_kind(info: zipfile.ZipInfo) -> str:
    """判断条目类型：'file' / 'dir'，特殊条目（符号链接等）直接拒绝。"""
    mode = (info.external_attr >> 16) & 0xFFFF
    fmt = stat.S_IFMT(mode)
    if fmt and fmt not in (stat.S_IFREG, stat.S_IFDIR):
        raise ZipRejected(
            f"unsupported entry type (symlink/device/pipe): {info.filename!r}"
        )
    if fmt == stat.S_IFDIR:
        return "dir"
    if fmt == stat.S_IFREG:
        return "file"
    # 无类型位（如 Windows 打包或仅含权限位）：按名字判断
    return "dir" if info.is_dir() else "file"


def validate_entries(zf: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """校验整个 ZIP，返回 {规范化文件路径: ZipInfo}（仅普通文件）。"""
    files: dict[str, zipfile.ZipInfo] = {}
    dirs: set[str] = set()
    seen: set[str] = set()
    declared_total = 0

    for info in zf.infolist():
        if info.flag_bits & 0x1:
            raise ZipRejected(f"encrypted entry not supported: {info.filename!r}")
        path, is_dir_name = _validate_name(info.filename)
        kind = _entry_kind(info)
        if path in seen:
            raise ZipRejected(f"duplicate entry name: {path!r}")
        seen.add(path)
        if kind == "dir" or is_dir_name:
            dirs.add(path)
        else:
            files[path] = info
            declared_total += info.file_size
            if declared_total > MAX_TOTAL_BYTES:
                raise ZipRejected(
                    f"extracted size exceeds 16 MiB limit (declared {declared_total} bytes)"
                )

    # 文件与目录路径冲突：'a' 既是文件又是目录，或某文件是另一路径的祖先
    for f in files:
        if f in dirs:
            raise ZipRejected(f"path used as both file and directory: {f!r}")
        parts = f.split("/")
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in files:
                raise ZipRejected(f"file used as directory by another entry: {f!r}")
    for d in dirs:
        parts = d.split("/")
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in files:
                raise ZipRejected(f"file used as directory by another entry: {d!r}")

    if "index.html" not in files:
        raise ZipRejected("archive must contain index.html at its root")
    return files


def extract_and_digest(
    zf: zipfile.ZipFile, files: dict[str, zipfile.ZipInfo], dest_dir: str
) -> tuple[str, list[tuple[str, int, str]], int]:
    """按排序后的路径解包到 dest_dir，同时计算版本摘要。

    返回 (版本摘要 hex, [(path, size, sha256), ...], 总字节数)。
    """
    version_hash = hashlib.sha256()
    manifest: list[tuple[str, int, str]] = []
    total = 0

    for path in sorted(files):
        info = files[path]
        version_hash.update(path.encode("utf-8"))
        version_hash.update(b"\x00")
        version_hash.update(struct.pack("<Q", info.file_size))

        file_hash = hashlib.sha256()
        dest = os.path.join(dest_dir, *path.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        size = 0
        try:
            with zf.open(info) as src, open(dest, "wb") as out:
                while True:
                    chunk = src.read(_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    total += len(chunk)
                    if total > MAX_TOTAL_BYTES:
                        raise ZipRejected("extracted size exceeds 16 MiB limit")
                    file_hash.update(chunk)
                    version_hash.update(chunk)
                    out.write(chunk)
        except ZipRejected:
            raise
        except (zipfile.BadZipFile, RuntimeError, EOFError) as exc:
            raise ZipRejected(f"corrupt archive member {path!r}: {exc}") from exc
        if size != info.file_size:
            raise ZipRejected(f"size mismatch for entry {path!r}")
        manifest.append((path, size, file_hash.hexdigest()))

    return version_hash.hexdigest(), manifest, total
