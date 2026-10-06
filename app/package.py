"""ZIP 发布包解析与校验。

规则（全部违反即拒绝，不产生任何可见版本）：
- 条目必须是普通文件或目录（拒绝符号链接、设备、管道等特殊条目）
- 路径以 "/" 分隔且为相对路径；拒绝空段、"."、".."、绝对路径、盘符、反斜杠、NUL
- 拒绝重复条目名
- 必须包含根级 index.html
- 解压后文件总量不得超过 MAX_TOTAL_BYTES（16 MiB）

版本摘要按排序后的相对路径及文件字节计算（SHA-256），内容相同摘要即相同。
"""

from __future__ import annotations

import hashlib
import os
import zipfile

# 解压总量硬上限 16 MiB
MAX_TOTAL_BYTES = 16 * 1024 * 1024
# 单文件读取分块
_CHUNK = 1024 * 1024


class PackageError(ValueError):
    """发布包不合法（映射为 HTTP 422）。"""


def _check_relative_name(name: str) -> None:
    """校验 ZIP 条目名：相对路径、/ 分隔、无空段/. /.. 、无反斜杠等。"""
    if name == "":
        raise PackageError("条目名为空")
    if "\x00" in name:
        raise PackageError(f"条目名包含 NUL: {name!r}")
    if "\\" in name:
        raise PackageError(f"路径不得包含反斜杠: {name!r}")
    # ZIP 目录条目通常以 "/" 结尾，先去掉结尾分隔再分段
    body = name[:-1] if name.endswith("/") else name
    if body == "":
        # 仅 "/" 之类
        raise PackageError("非法路径: /")
    if body.startswith("/"):
        raise PackageError(f"路径必须是相对路径: {name!r}")
    if ":" in body.split("/", 1)[0]:
        # Windows 盘符（a:/...）
        raise PackageError(f"路径不得包含盘符: {name!r}")
    for segment in body.split("/"):
        if segment == "":
            raise PackageError(f"路径包含空段: {name!r}")
        if segment == "." or segment == "..":
            raise PackageError(f"路径不得包含 . 或 .. 段: {name!r}")


def _safe_join(base: str, rel_path: str) -> str:
    """把相对路径拼接到基础目录，并二次确认结果仍在基础目录内。"""
    target = os.path.normpath(os.path.join(base, rel_path))
    base_abs = os.path.abspath(base)
    target_abs = os.path.abspath(target)
    if target_abs != base_abs and not target_abs.startswith(base_abs + os.sep):
        raise PackageError(f"路径越界: {rel_path!r}")
    return target_abs


def _extract_one(
    package: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    dest_dir: str,
    dir_paths: set[str],
    file_paths: set[str],
    seen: set[str],
) -> tuple[int, str] | None:
    """校验并解压单个条目。

    目录条目返回 None；普通文件返回 ``(写入字节数, 相对路径)``。
    超出大小上限、路径非法、类型非法等抛 PackageError。
    """
    name = info.filename
    _check_relative_name(name)
    if name in seen:
        raise PackageError(f"重复条目: {name!r}")
    seen.add(name)

    is_dir = info.is_dir()

    # 结构判定：除 info.is_dir() 外，再以 unix mode 复核
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    if unix_mode != 0:
        unix_type = unix_mode & 0o170000
        if unix_type == 0o120000:
            raise PackageError(f"拒绝符号链接条目: {name!r}")
        if unix_type not in (0o040000, 0o100000, 0):
            raise PackageError(f"只允许普通文件或目录: {name!r}")
        if unix_type == 0o040000:
            is_dir = True
        elif unix_type == 0o100000:
            is_dir = False

    rel = name[:-1] if is_dir else name
    segments = rel.split("/")

    # 与已有条目的类型冲突检查
    if is_dir:
        if rel in file_paths:
            raise PackageError(f"同名条目既是目录又是文件: {name!r}")
        dir_paths.add(rel)
    else:
        if rel in dir_paths:
            raise PackageError(f"同名条目既是文件又是目录: {name!r}")
        file_paths.add(rel)
    # 父目录与文件冲突：a（文件）之后出现 a/b
    for i in range(len(segments) - 1):
        ancestor = "/".join(segments[: i + 1])
        if ancestor in file_paths:
            raise PackageError(f"父路径已被普通文件占用: {name!r}")

    target = _safe_join(dest_dir, rel)
    if is_dir:
        os.makedirs(target, exist_ok=True)
        return None

    if info.file_size > MAX_TOTAL_BYTES:
        raise PackageError(f"解压总量超过 16 MiB: {name!r}")

    os.makedirs(os.path.dirname(target), exist_ok=True)
    written = 0
    with package.open(info, "r") as src, open(target, "wb") as out:
        while True:
            chunk = src.read(_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_TOTAL_BYTES:
                raise PackageError("解压总量超过 16 MiB")
            out.write(chunk)
    return written, rel


def unpack_and_validate(package: zipfile.ZipFile, dest_dir: str) -> tuple[str, list[tuple[str, int]]]:
    """校验并解压 ZIP 到 dest_dir。

    成功返回 ``(digest, files)``：
    - digest：按排序后路径及文件字节计算的 SHA-256（十六进制）
    - files：``[(相对路径, 字节数), ...]``（按路径排序）
    """
    infos = package.infolist()
    if not infos:
        raise PackageError("空 ZIP 包")

    seen: set[str] = set()
    total = 0
    has_index = False
    # 目录集合，用于检测“同名既是文件又是目录”的冲突
    dir_paths: set[str] = set()
    file_paths: set[str] = set()

    # 第一段：逐条校验并解压
    for info in infos:
        try:
            result = _extract_one(
                package, info, dest_dir, dir_paths, file_paths, seen
            )
        except PackageError:
            raise
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            # 磁盘满、损坏的压缩数据等都视为非法包
            raise PackageError(f"条目 {info.filename!r} 无法解压: {exc}") from exc
        if result is None:  # 目录条目
            continue
        written, rel = result
        total += written
        if total > MAX_TOTAL_BYTES:
            raise PackageError("解压总量超过 16 MiB")
        if rel == "index.html":
            has_index = True

    if not has_index:
        raise PackageError("缺少 index.html")

    # 第二段：以磁盘上的实际文件为准，按排序后路径 + 字节计算摘要。
    # 只纳入清单中的普通文件；摘要不依赖 mtime/权限/空目录。
    files: list[tuple[str, int]] = []
    digest = hashlib.sha256()
    digest.update(b"STATIC-SITE-V1\n")
    for rel in sorted(file_paths):
        target = _safe_join(dest_dir, rel)
        size = 0
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        with open(target, "rb") as fh:
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
        digest.update(b"\0")
        files.append((rel, size))

    return digest.hexdigest(), files
