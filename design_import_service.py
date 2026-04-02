from __future__ import annotations

import hashlib
from dataclasses import dataclass


SUPPORTED_EXTS = {"png", "jpg", "jpeg", "webp", "pdf", "fig", "sketch"}
MAX_BATCH_BYTES = 500 * 1024 * 1024  # 500MB


@dataclass
class ImportCandidate:
    name: str
    data: bytes
    ext: str
    file_hash: str


def normalize_ext(name: str) -> str:
    return name.lower().rsplit(".", 1)[-1] if "." in name else ""


def build_candidates(
    files: list[dict[str, bytes]],
    max_batch_bytes: int = MAX_BATCH_BYTES,
) -> tuple[list[ImportCandidate], list[dict[str, str]], int]:
    """将上传缓存转为可导入候选，并返回失败清单与总大小。

    返回：(candidates, failed_items, total_size_bytes)
    failed_items 的元素结构：{"name": str, "reason": str}
    """
    failed_items: list[dict[str, str]] = []
    candidates: list[ImportCandidate] = []
    total_size_bytes = 0
    seen_hashes: set[str] = set()

    for item in files:
        name = str(item.get("name") or "设计图")
        data = item.get("bytes") or b""
        if not isinstance(data, bytes):
            failed_items.append({"name": name, "reason": "文件读取失败"})
            continue
        total_size_bytes += len(data)
        ext = normalize_ext(name)
        if ext not in SUPPORTED_EXTS:
            failed_items.append({"name": name, "reason": "格式不支持"})
            continue
        file_hash = hashlib.sha256(data).hexdigest()
        # 批次内重复直接跳过，避免重复解析同一内容
        if file_hash in seen_hashes:
            failed_items.append({"name": name, "reason": "批次内重复文件"})
            continue
        seen_hashes.add(file_hash)
        candidates.append(
            ImportCandidate(name=name, data=data, ext=ext, file_hash=file_hash)
        )

    if total_size_bytes > max_batch_bytes:
        return [], [{"name": "本次导入", "reason": "总大小超过 500MB"}], total_size_bytes

    return candidates, failed_items, total_size_bytes

