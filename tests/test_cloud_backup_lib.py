# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "cloud_backup"))

from backup_lib import (  # type: ignore[import-not-found]
    build_manifest_body,
    mask_database_url,
    new_backup_run_id,
    parse_dataset_prefixes,
    resolve_pg_dump_binary,
    size_drop_blocks_new_dump,
    validate_completed_manifest,
)


def test_mask_database_url_hides_password():
    u = "postgresql://user:secret@ep-neon.aws.neon.tech/neondb?sslmode=require"
    m = mask_database_url(u)
    assert "secret" not in m
    assert "****" in m or "user@" in m


def test_parse_dataset_prefixes():
    assert parse_dataset_prefixes("") == []
    assert parse_dataset_prefixes("a,b/") == ["a/", "b/"]


def test_validate_completed_manifest_ok():
    m = build_manifest_body(
        backup_run_id="x",
        postgres={
            "object_key": "backups/pg/x/p.sql.gz",
            "sha256": "ab" * 32,
            "size_bytes": 10,
            "completed_at_utc": "2026-01-01T00:00:00Z",
        },
        r2_dataset={
            "mode": "manifest_only",
            "object_count": 0,
            "completed_at_utc": "2026-01-01T00:00:00Z",
        },
        steps={},
        deployment={"env": "prod", "db_host": "x"},
    )
    m["run_status"] = "completed"
    ok, err = validate_completed_manifest(m)
    assert ok, err


def test_validate_completed_manifest_rejects_pending():
    m = build_manifest_body(
        backup_run_id="x",
        postgres={
            "object_key": "k",
            "sha256": "a" * 64,
            "size_bytes": 1,
            "completed_at_utc": "2026-01-01T00:00:00Z",
        },
        r2_dataset={"mode": "manifest_only", "object_count": 0, "completed_at_utc": "2026-01-01T00:00:00Z"},
        steps={},
        deployment={"env": "prod", "db_host": "x"},
    )
    m["run_status"] = "failed"
    ok, _ = validate_completed_manifest(m)
    assert not ok


def test_size_drop_blocks():
    assert size_drop_blocks_new_dump(100, None, 30.0) == (False, "")
    assert size_drop_blocks_new_dump(100, 100, 30.0) == (False, "")
    blocked, msg = size_drop_blocks_new_dump(50, 100, 30.0)
    assert blocked and "50" in msg


def test_new_backup_run_id_format():
    rid = new_backup_run_id()
    assert "_" in rid
    assert len(rid) > 12


def test_resolve_pg_dump_binary_env_override(monkeypatch):
    monkeypatch.setenv("PG_DUMP", "/custom/pg_dump")
    assert resolve_pg_dump_binary() == "/custom/pg_dump"


def test_resolve_pg_dump_binary_falls_back_to_which():
    import shutil

    w = shutil.which("pg_dump")
    if w:
        assert resolve_pg_dump_binary() == w
    else:
        assert resolve_pg_dump_binary() == "pg_dump"
