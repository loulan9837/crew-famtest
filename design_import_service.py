from __future__ import annotations

import hashlib
import posixpath
import zipfile
from dataclasses import dataclass
from io import BytesIO


# 解压后参与解析的扩展名（不含 zip 容器本身）
SUPPORTED_EXTS = {"png", "jpg", "jpeg", "webp", "pdf", "fig", "sketch"}
ZIP_EXT = "zip"
MAX_BATCH_BYTES = 1024 * 1024 * 1024  # 1GB（解压后合计）
MAX_FILES_PER_ZIP = 500
# 与 app_ui 单图上限对齐，防止 ZIP 内超大文件撑爆内存
MAX_SINGLE_UNCOMPRESSED_BYTES = 30 * 1024 * 1024


@dataclass
class ImportCandidate:
    name: str
    data: bytes
    ext: str
    file_hash: str


def normalize_ext(name: str) -> str:
    return name.lower().rsplit(".", 1)[-1] if "." in name else ""


def _is_safe_zip_member(filename: str) -> bool:
    """防御 ZIP 路径穿越：仅允许相对路径且不包含 .. 段。"""
    n = (filename or "").replace("\\", "/")
    if not n or n.startswith("/"):
        return False
    parts = [p for p in n.split("/") if p]
    if ".." in parts:
        return False
    return True


def _expand_zip_to_items(zip_label: str, data: bytes) -> tuple[list[dict[str, bytes]], list[dict[str, str]]]:
    """将单个 ZIP 展开为 {name, bytes} 列表；name 形如 `包名/内部文件名`。"""
    out: list[dict[str, bytes]] = []
    failed: list[dict[str, str]] = []
    try:
        zf = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as e:
        return [], [{"name": zip_label, "reason": f"ZIP 损坏或不是有效压缩包: {e}"}]
    except Exception as e:
        return [], [{"name": zip_label, "reason": f"ZIP 无法打开: {e}"}]

    count = 0
    truncated = False
    try:
        for zi in zf.infolist():
            if zi.is_dir():
                continue
            if count >= MAX_FILES_PER_ZIP:
                truncated = True
                break
            if not _is_safe_zip_member(zi.filename):
                continue
            base = posixpath.basename(zi.filename.replace("\\", "/"))
            if not base:
                continue
            ext = normalize_ext(base)
            if ext not in SUPPORTED_EXTS:
                continue
            try:
                raw = zf.read(zi.filename)
            except Exception as e:
                failed.append({"name": f"{zip_label}/{base}", "reason": str(e)})
                continue
            if len(raw) > MAX_SINGLE_UNCOMPRESSED_BYTES:
                failed.append(
                    {
                        "name": f"{zip_label}/{base}",
                        "reason": f"解压后单文件超过 {MAX_SINGLE_UNCOMPRESSED_BYTES // (1024 * 1024)}MB",
                    }
                )
                continue
            display_name = f"{zip_label}/{base}"
            out.append({"name": display_name, "bytes": raw})
            count += 1
    finally:
        zf.close()

    if truncated:
        failed.append(
            {
                "name": zip_label,
                "reason": f"ZIP 内仅处理前 {MAX_FILES_PER_ZIP} 个支持的文件",
            }
        )
    if not out and not failed:
        failed.append({"name": zip_label, "reason": "ZIP 内无支持的图片/PDF 文件（PNG/JPG/WEBP/PDF/FIG/SKETCH）"})
    return out, failed


def _flatten_upload_items(
    files: list[dict[str, bytes]],
) -> tuple[list[dict[str, bytes]], list[dict[str, str]]]:
    """将顶层上传项展开：ZIP → 多文件，其余合法扩展原样保留。"""
    flattened: list[dict[str, bytes]] = []
    failed_items: list[dict[str, str]] = []

    for item in files:
        name = str(item.get("name") or "设计图")
        data = item.get("bytes") or b""
        if not isinstance(data, bytes):
            failed_items.append({"name": name, "reason": "文件读取失败"})
            continue
        ext = normalize_ext(name)
        if ext == ZIP_EXT:
            inner, zip_fails = _expand_zip_to_items(name, data)
            failed_items.extend(zip_fails)
            flattened.extend(inner)
        elif ext in SUPPORTED_EXTS:
            if len(data) > MAX_SINGLE_UNCOMPRESSED_BYTES:
                failed_items.append(
                    {
                        "name": name,
                        "reason": f"单文件超过 {MAX_SINGLE_UNCOMPRESSED_BYTES // (1024 * 1024)}MB",
                    }
                )
                continue
            flattened.append({"name": name, "bytes": data})
        else:
            failed_items.append({"name": name, "reason": "格式不支持"})

    return flattened, failed_items


def build_candidates(
    files: list[dict[str, bytes]],
    max_batch_bytes: int = MAX_BATCH_BYTES,
) -> tuple[list[ImportCandidate], list[dict[str, str]], int]:
    """将上传项（含 ZIP）转为可导入候选，并返回失败清单与总字节数。

    返回：(candidates, failed_items, total_size_bytes)
    failed_items 的元素结构：{"name": str, "reason": str}
    """
    flattened, pre_failed = _flatten_upload_items(files)
    failed_items: list[dict[str, str]] = list(pre_failed)
    candidates: list[ImportCandidate] = []
    total_size_bytes = sum(len(x.get("bytes") or b"") for x in flattened)
    seen_hashes: set[str] = set()

    if total_size_bytes > max_batch_bytes:
        limit_gb = max(1, max_batch_bytes // (1024 * 1024 * 1024))
        return (
            [],
            failed_items + [{"name": "本次导入", "reason": f"解压后总大小超过 {limit_gb}GB"}],
            total_size_bytes,
        )

    for item in flattened:
        name = str(item.get("name") or "设计图")
        data = item.get("bytes") or b""
        if not isinstance(data, bytes) or not data:
            failed_items.append({"name": name, "reason": "文件内容为空"})
            continue
        ext = normalize_ext(name)
        if ext not in SUPPORTED_EXTS:
            failed_items.append({"name": name, "reason": "格式不支持"})
            continue
        file_hash = hashlib.sha256(data).hexdigest()
        if file_hash in seen_hashes:
            failed_items.append({"name": name, "reason": "批次内重复文件"})
            continue
        seen_hashes.add(file_hash)
        candidates.append(
            ImportCandidate(name=name, data=data, ext=ext, file_hash=file_hash)
        )

    return candidates, failed_items, total_size_bytes
