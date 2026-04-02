# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.cloud_memory import (  # type: ignore[import-untyped]
    _content_hash,
    _normalize_cloud_project_id,
    _normalize_r2_key_prefix,
    _safe_file_component,
    _sanitize_source_type,
    get_database_url_for_app,
    is_cloud_memory_configured,
)


def test_content_hash_stable():
    assert _content_hash("a") == _content_hash("a")
    assert _content_hash("a") != _content_hash("b")


def test_safe_file_component():
    assert ".." not in _safe_file_component("../../../etc/passwd")
    assert _safe_file_component("x" * 300) == "x" * 180


def test_normalize_cloud_project_id():
    assert _normalize_cloud_project_id("RM11") == "RM11"
    assert _normalize_cloud_project_id("evil") == "FAMBASE"
    assert _normalize_cloud_project_id(None) == "FAMBASE"


def test_normalize_r2_key_prefix():
    assert _normalize_r2_key_prefix("uploads") == "uploads"
    assert _normalize_r2_key_prefix("../evil") == "uploads"
    assert _normalize_r2_key_prefix("assets/v1") == "assets/v1"


def test_sanitize_source_type():
    assert _sanitize_source_type("design_mockup") == "design_mockup"
    assert ";" not in _sanitize_source_type("a;b")


def test_not_configured_without_env(monkeypatch):
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_APP", raising=False)
    monkeypatch.delenv("R2_ACCOUNT_ID", raising=False)
    assert get_database_url_for_app() == ""
    assert is_cloud_memory_configured() is False
