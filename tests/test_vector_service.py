# -*- coding: utf-8 -*-
"""vector_service：切块与回退格式（不依赖真实 Neon/Gemini）。"""
from __future__ import annotations

import pytest

from utils import vector_service as vs


def test_chunks_from_table_rows():
    md = """| ID | 模块 | 预期 |
| --- | --- | --- |
| 1 | 直播 | 正常 |
| 2 | 聊天 | 提示 |
"""
    pairs = vs._chunks_from_full_regression_markdown(md)
    assert len(pairs) >= 2
    assert all(len(t) <= vs.MAX_CHUNK_CHARS + 100 for _, t in pairs)


def test_chunks_fallback_paragraph():
    md = "第一段说明文字足够长用于测试段落切块。" * 5
    pairs = vs._chunks_from_full_regression_markdown(md)
    assert pairs


def test_format_fallback_truncates():
    long_fb = "x" * (vs.FALLBACK_MAX_CHARS + 5000)
    out = vs._format_fallback_block(long_fb)
    assert "截断" in out or "..." in out
    assert len(out) < len(long_fb)


def test_normalize_project_id():
    assert vs._normalize_project_id("rm11") == "RM11"
    assert vs._normalize_project_id("other") == "FAMBASE"


def test_aggregate_hash_stable():
    assert vs._aggregate_content_hash("a") == vs._aggregate_content_hash("a")
    assert vs._aggregate_content_hash("a") != vs._aggregate_content_hash("b")
