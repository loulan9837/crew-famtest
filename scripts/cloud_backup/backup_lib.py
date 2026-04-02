# -*- coding: utf-8 -*-
"""
冷备单次 run：Postgres dump → R2、业务对象清单/镜像 → manifest → 校验。
对齐 docs/数据持久化与自动备份-方案.md 第二章成功口径与 manifest 字段。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

MANIFEST_SCHEMA_VERSION = 1
DEFAULT_BACKUP_PREFIX = "backups"
DEFAULT_RETENTION = 7
DEFAULT_SIZE_DROP_PCT = 30.0


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_backup_run_id() -> str:
    base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{base}_{secrets.token_hex(4)}"


def sha256_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def mask_database_url(url: str) -> str:
    """日志脱敏：隐藏口令。"""
    if not url or "@" not in url:
        return "(empty)"
    try:
        p = urllib.parse.urlsplit(url.replace("postgresql+asyncpg://", "postgresql://", 1))
        if p.password:
            netloc = p.netloc.replace(f":{p.password}@", ":****@", 1)
            return urllib.parse.urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))
    except Exception:
        pass
    return re.sub(r":([^:@/]+)@", r":****@", url, count=1)


def normalize_backup_prefix(prefix: str) -> str:
    p = (prefix or DEFAULT_BACKUP_PREFIX).strip().strip("/")
    return p or DEFAULT_BACKUP_PREFIX


def parse_dataset_prefixes(raw: str | None) -> list[str]:
    if not raw or not raw.strip():
        return []
    return [x.strip().rstrip("/") + "/" if x.strip() and not x.strip().endswith("/") else x.strip() for x in raw.split(",") if x.strip()]


def build_manifest_body(
    *,
    backup_run_id: str,
    postgres: dict[str, Any],
    r2_dataset: dict[str, Any],
    steps: dict[str, Any],
    deployment: dict[str, Any],
    notes: str = "",
) -> dict[str, Any]:
    return {
        "backup_run_id": backup_run_id,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "created_at_utc": utc_now_iso(),
        "run_status": "pending",
        "postgres": postgres,
        "r2_dataset": r2_dataset,
        "steps": steps,
        "deployment": deployment,
        "notes": notes or "",
    }


def validate_completed_manifest(m: dict[str, Any]) -> tuple[bool, str]:
    """校验已完成 run 的 manifest 必填字段（恢复 Runbook 第一步）。"""
    if m.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        return False, "schema_version mismatch"
    if not m.get("backup_run_id"):
        return False, "missing backup_run_id"
    if m.get("run_status") != "completed":
        return False, "run_status not completed"
    pg = m.get("postgres") or {}
    for k in ("object_key", "sha256", "size_bytes", "completed_at_utc"):
        if k not in pg:
            return False, f"postgres missing {k}"
    r2 = m.get("r2_dataset") or {}
    for k in ("mode", "object_count", "completed_at_utc"):
        if k not in r2:
            return False, f"r2_dataset missing {k}"
    return True, ""


def size_drop_blocks_new_dump(
    new_size: int,
    previous_size: int | None,
    threshold_pct: float,
) -> tuple[bool, str]:
    """体积跌落熔断：新 dump 较上一份成功 dump 下降超过阈值则阻断。"""
    if previous_size is None or previous_size <= 0:
        return False, ""
    if new_size >= previous_size:
        return False, ""
    drop_pct = (1.0 - (new_size / float(previous_size))) * 100.0
    if drop_pct > threshold_pct:
        return True, (
            f"postgres dump size dropped {drop_pct:.1f}% vs last good "
            f"({new_size} < {previous_size}, threshold {threshold_pct}%)"
        )
    return False, ""


def read_override_token() -> str:
    return (os.environ.get("BACKUP_ALLOW_SIZE_DROP_OVERRIDE") or "").strip()


@dataclass
class BackupConfig:
    database_url: str
    r2_endpoint: str
    r2_access_key: str
    r2_secret_key: str
    r2_bucket: str
    backup_prefix: str = DEFAULT_BACKUP_PREFIX
    dataset_prefixes: list[str] = field(default_factory=list)
    dataset_mode: str = "manifest_only"  # manifest_only | prefix_copy
    retention_completed: int = DEFAULT_RETENTION
    size_drop_pct: float = DEFAULT_SIZE_DROP_PCT
    deployment_env: str = "prod"
    db_host_hint: str = ""  # 非敏感标识，如 neon project id 片段
    notes: str = ""


def _s3_client(cfg: BackupConfig):
    import boto3  # type: ignore[import-untyped]
    from botocore.config import Config  # type: ignore[import-untyped]

    return boto3.client(
        "s3",
        endpoint_url=cfg.r2_endpoint.rstrip("/"),
        aws_access_key_id=cfg.r2_access_key,
        aws_secret_access_key=cfg.r2_secret_key,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def _key_join(prefix: str, *parts: str) -> str:
    base = prefix.strip("/")
    rest = "/".join(p.strip("/") for p in parts if p)
    return f"{base}/{rest}" if base else rest


def pg_dump_gzip(database_url: str, out_gz_path: str, log: Callable[[str], None]) -> None:
    """使用 pg_dump 明文格式经 gzip 压缩（便于跨版本与 inspect）。"""
    masked = mask_database_url(database_url)
    log(f"pg_dump starting (db={masked})")
    with gzip.open(out_gz_path, "wb", compresslevel=6) as gz:
        proc = subprocess.run(
            ["pg_dump", "--no-owner", "--no-acl", database_url],
            stdout=gz,
            stderr=subprocess.PIPE,
            check=False,
        )
    if proc.returncode != 0:
        msg = (proc.stderr or b"").decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"pg_dump failed ({proc.returncode}): {msg}")


def s3_upload_file_verify(
    client: Any,
    bucket: str,
    local_path: str,
    key: str,
    log: Callable[[str], None],
) -> tuple[int, str]:
    size = os.path.getsize(local_path)
    sha = sha256_file(local_path)
    client.upload_file(Filename=local_path, Bucket=bucket, Key=key)
    head = client.head_object(Bucket=bucket, Key=key)
    cl = int(head.get("ContentLength") or 0)
    if cl != size:
        raise RuntimeError(f"size mismatch after upload: head={cl} local={size}")
    log(f"upload ok key={key} size={size}")
    return size, sha


def list_objects_under_prefixes(
    client: Any,
    bucket: str,
    prefixes: list[str],
    log: Callable[[str], None],
    exclude_key_prefix: str = "",
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    ex = (exclude_key_prefix or "").strip()
    for prefix in prefixes:
        pfx = prefix if prefix.endswith("/") or not prefix else prefix + "/"
        token = None
        while True:
            kw: dict[str, Any] = {"Bucket": bucket, "Prefix": pfx, "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            resp = client.list_objects_v2(**kw)
            for obj in resp.get("Contents") or []:
                k = obj.get("Key") or ""
                if k.endswith("/"):
                    continue
                if ex and k.startswith(ex):
                    continue
                out.append(
                    {
                        "key": k,
                        "size_bytes": int(obj.get("Size") or 0),
                        "etag": (obj.get("ETag") or "").strip('"'),
                    }
                )
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        log(f"listed prefix={pfx} cumulative_objects={len(out)}")
    return out


def mirror_objects_copy(
    client: Any,
    bucket: str,
    objects: list[dict[str, Any]],
    dest_prefix: str,
    log: Callable[[str], None],
) -> None:
    """同桶 server-side CopyObject，将业务对象复制到冷备前缀下。"""
    for o in objects:
        src_key = o["key"]
        dest_key = _key_join(dest_prefix, src_key)
        client.copy_object(
            Bucket=bucket,
            Key=dest_key,
            CopySource={"Bucket": bucket, "Key": src_key},
            MetadataDirective="COPY",
        )
        head = client.head_object(Bucket=bucket, Key=dest_key)
        if int(head.get("ContentLength") or 0) != int(o["size_bytes"]):
            raise RuntimeError(f"copy size mismatch {src_key} -> {dest_key}")
    log(f"mirror copy done count={len(objects)}")


def fetch_latest_completed_manifest(
    client: Any,
    bucket: str,
    backup_prefix: str,
    log: Callable[[str], None],
) -> dict[str, Any] | None:
    from botocore.exceptions import ClientError

    key = _key_join(backup_prefix, "manifests", "latest_completed.json")
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
        body = resp["Body"].read()
        data = json.loads(body.decode("utf-8"))
        mid = data.get("backup_run_id")
        if not mid:
            return None
        mkey = _key_join(backup_prefix, "manifests", f"{mid}.json")
        resp2 = client.get_object(Bucket=bucket, Key=mkey)
        m = json.loads(resp2["Body"].read().decode("utf-8"))
        ok, _ = validate_completed_manifest(m)
        if not ok:
            log("latest_completed pointer invalid manifest")
            return None
        return m
    except ClientError as e:
        code = (e.response.get("Error") or {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            return None
        log(f"no previous completed manifest: {e}")
        return None
    except Exception as e:
        log(f"no previous completed manifest: {e}")
        return None


def list_manifest_keys(
    client: Any,
    bucket: str,
    backup_prefix: str,
) -> list[str]:
    keys: list[str] = []
    pfx = _key_join(backup_prefix, "manifests") + "/"
    token = None
    while True:
        kw: dict[str, Any] = {"Bucket": bucket, "Prefix": pfx, "MaxKeys": 500}
        if token:
            kw["ContinuationToken"] = token
        resp = client.list_objects_v2(**kw)
        for obj in resp.get("Contents") or []:
            k = obj.get("Key") or ""
            if k.endswith(".json") and "/manifests/" in k:
                name = k.rsplit("/", 1)[-1]
                if name not in ("latest_completed.json",):
                    keys.append(k)
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return keys


def load_manifest_from_key(client: Any, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
        return json.loads(resp["Body"].read().decode("utf-8"))
    except Exception:
        return None


def apply_retention(
    client: Any,
    cfg: BackupConfig,
    log: Callable[[str], None],
) -> None:
    """仅删除 run_status=completed 且超出保留数量的最旧 run（先写后删：方案 §7）。"""
    keys = list_manifest_keys(client, cfg.r2_bucket, cfg.backup_prefix)
    manifests: list[tuple[str, dict[str, Any]]] = []
    for k in keys:
        m = load_manifest_from_key(client, cfg.r2_bucket, k)
        if m and m.get("run_status") == "completed":
            manifests.append((k, m))
    manifests.sort(key=lambda x: x[1].get("created_at_utc") or "")
    keep = max(1, int(cfg.retention_completed))
    if len(manifests) <= keep:
        log(f"retention: {len(manifests)} completed, nothing to prune")
        return
    to_drop = manifests[: len(manifests) - keep]
    for _, m in to_drop:
        rid = m.get("backup_run_id")
        if not rid:
            continue
        pg_prefix = _key_join(cfg.backup_prefix, "pg", rid)
        mirror_prefix = _key_join(cfg.backup_prefix, "r2_mirror", rid)
        inv_key = (m.get("r2_dataset") or {}).get("inventory_object_key")
        mkey = _key_join(cfg.backup_prefix, "manifests", f"{rid}.json")
        for prefix in (pg_prefix, mirror_prefix):
            _delete_prefix(client, cfg.r2_bucket, prefix + "/", log)
        for ek in filter(None, [inv_key, mkey]):
            try:
                client.delete_object(Bucket=cfg.r2_bucket, Key=ek)
                log(f"retention deleted {ek}")
            except Exception as e:
                log(f"retention delete fail {ek}: {e}")
    log(f"retention pruned {len(to_drop)} old completed runs")
    _refresh_latest_completed_pointer(client, cfg, log)


def _refresh_latest_completed_pointer(client: Any, cfg: BackupConfig, log: Callable[[str], None]) -> None:
    """淘汰后若指针指向已删 manifest，则回写到当前最新 completed。"""
    keys = list_manifest_keys(client, cfg.r2_bucket, cfg.backup_prefix)
    completed: list[dict[str, Any]] = []
    for k in keys:
        m = load_manifest_from_key(client, cfg.r2_bucket, k)
        if m and m.get("run_status") == "completed":
            ok, _ = validate_completed_manifest(m)
            if ok:
                completed.append(m)
    if not completed:
        log("retention: no completed manifests left; latest_completed not updated")
        return
    completed.sort(key=lambda x: x.get("created_at_utc") or "")
    best = completed[-1]
    rid = best.get("backup_run_id")
    if not rid:
        return
    mkey = _key_join(cfg.backup_prefix, "manifests", f"{rid}.json")
    ptr = {
        "backup_run_id": rid,
        "manifest_object_key": mkey,
        "updated_at_utc": utc_now_iso(),
    }
    ptr_key = _key_join(cfg.backup_prefix, "manifests", "latest_completed.json")
    client.put_object(
        Bucket=cfg.r2_bucket,
        Key=ptr_key,
        Body=json.dumps(ptr, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    log(f"retention: refreshed latest_completed -> {rid}")


def _delete_prefix(client: Any, bucket: str, prefix: str, log: Callable[[str], None]) -> None:
    token = None
    while True:
        kw: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 500}
        if token:
            kw["ContinuationToken"] = token
        resp = client.list_objects_v2(**kw)
        objs = [{"Key": o["Key"]} for o in (resp.get("Contents") or [])]
        if objs:
            client.delete_objects(Bucket=bucket, Delete={"Objects": objs})
            log(f"deleted {len(objs)} objects under {prefix}")
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")


def run_backup(cfg: BackupConfig, log: Callable[[str], None]) -> int:
    """
    执行单次备份 run。返回进程退出码：0=completed，1=failed，2=skipped（配置缺失）。
    """
    if not cfg.database_url.strip():
        log("DATABASE_URL_BACKUP empty — skip (本地/未配置云端库)")
        return 2
    if not all([cfg.r2_endpoint, cfg.r2_access_key, cfg.r2_secret_key, cfg.r2_bucket]):
        log("R2 configuration incomplete — skip")
        return 2

    backup_run_id = new_backup_run_id()
    steps: dict[str, Any] = {
        "pg": {"status": "pending", "completed_at_utc": None},
        "r2": {"status": "pending", "completed_at_utc": None},
        "verify": {"status": "pending", "completed_at_utc": None},
    }
    deployment = {
        "env": cfg.deployment_env,
        "db_host": cfg.db_host_hint or "(redacted)",
    }

    client = _s3_client(cfg)
    prev = fetch_latest_completed_manifest(client, cfg.r2_bucket, cfg.backup_prefix, log)
    prev_pg_size = None
    if prev:
        prev_pg_size = int((prev.get("postgres") or {}).get("size_bytes") or 0)

    fd, gz_path = tempfile.mkstemp(suffix=".sql.gz")
    os.close(fd)
    manifest: dict[str, Any] | None = None

    try:
        # --- PG ---
        try:
            pg_dump_gzip(cfg.database_url, gz_path, log)
        except Exception as e:
            log(f"pg step failed: {e}")
            steps["pg"] = {"status": "failed", "error": str(e)[:500], "completed_at_utc": utc_now_iso()}
            manifest = build_manifest_body(
                backup_run_id=backup_run_id,
                postgres={},
                r2_dataset={
                    "mode": cfg.dataset_mode,
                    "prefix_or_keys": cfg.dataset_prefixes,
                    "object_count": 0,
                    "completed_at_utc": utc_now_iso(),
                },
                steps=steps,
                deployment=deployment,
                notes=cfg.notes,
            )
            manifest["run_status"] = "failed"
            mk = _key_join(cfg.backup_prefix, "manifests", f"{backup_run_id}.json")
            raw_fail = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            client.put_object(Bucket=cfg.r2_bucket, Key=mk, Body=raw_fail, ContentType="application/json")
            return 1

        gz_size = os.path.getsize(gz_path)
        blocked, block_msg = size_drop_blocks_new_dump(gz_size, prev_pg_size, cfg.size_drop_pct)
        if blocked:
            tok = read_override_token()
            if tok != "OVERRIDE_SIZE_DROP_CONFIRMED":
                log(f"SIZE DROP CIRCUIT: {block_msg}")
                steps["pg"] = {"status": "failed", "error": block_msg, "completed_at_utc": utc_now_iso()}
                manifest = build_manifest_body(
                    backup_run_id=backup_run_id,
                    postgres={"size_bytes": gz_size},
                    r2_dataset={
                        "mode": cfg.dataset_mode,
                        "prefix_or_keys": cfg.dataset_prefixes,
                        "object_count": 0,
                        "completed_at_utc": utc_now_iso(),
                    },
                    steps=steps,
                    deployment=deployment,
                    notes=cfg.notes,
                )
                manifest["run_status"] = "failed"
                mk = _key_join(cfg.backup_prefix, "manifests", f"{backup_run_id}.json")
                client.put_object(
                    Bucket=cfg.r2_bucket,
                    Key=mk,
                    Body=json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                    ContentType="application/json",
                )
                return 1
            log("SIZE DROP OVERRIDE used — audit required (BACKUP_ALLOW_SIZE_DROP_OVERRIDE)")

        pg_key = _key_join(cfg.backup_prefix, "pg", backup_run_id, "postgres.sql.gz")
        pg_size, pg_sha = s3_upload_file_verify(client, cfg.r2_bucket, gz_path, pg_key, log)
        steps["pg"] = {"status": "completed", "completed_at_utc": utc_now_iso()}
        postgres_meta = {
            "object_key": pg_key,
            "sha256": pg_sha,
            "size_bytes": pg_size,
            "completed_at_utc": steps["pg"]["completed_at_utc"],
        }

        # --- R2 dataset ---
        inv_key = ""
        inv_sha = ""
        inv_size = 0
        object_count = 0
        mirror_prefix = _key_join(cfg.backup_prefix, "r2_mirror", backup_run_id)

        if not cfg.dataset_prefixes:
            r2_meta = {
                "mode": "manifest_only",
                "prefix_or_keys": [],
                "object_count": 0,
                "completed_at_utc": utc_now_iso(),
                "note": "no R2_DATASET_PREFIXES configured",
            }
            steps["r2"] = {"status": "completed", "completed_at_utc": utc_now_iso()}
        else:
            ex_pfx = _key_join(cfg.backup_prefix, "") + "/"
            objects = list_objects_under_prefixes(
                client, cfg.r2_bucket, cfg.dataset_prefixes, log, exclude_key_prefix=ex_pfx
            )
            object_count = len(objects)
            inv_body = {"backup_run_id": backup_run_id, "objects": objects}
            inv_raw = json.dumps(inv_body, ensure_ascii=False, indent=2).encode("utf-8")
            inv_size = len(inv_raw)
            inv_sha = sha256_bytes(inv_raw)
            inv_key = _key_join(cfg.backup_prefix, "inventory", f"{backup_run_id}.json")
            client.put_object(Bucket=cfg.r2_bucket, Key=inv_key, Body=inv_raw, ContentType="application/json")
            head_inv = client.head_object(Bucket=cfg.r2_bucket, Key=inv_key)
            if int(head_inv.get("ContentLength") or 0) != inv_size:
                raise RuntimeError("inventory upload size mismatch")

            if cfg.dataset_mode == "prefix_copy":
                mirror_objects_copy(client, cfg.r2_bucket, objects, mirror_prefix, log)

            r2_meta = {
                "mode": cfg.dataset_mode,
                "prefix_or_keys": cfg.dataset_prefixes,
                "object_count": object_count,
                "inventory_object_key": inv_key,
                "inventory_sha256": inv_sha,
                "inventory_size_bytes": inv_size,
                "mirror_prefix": mirror_prefix if cfg.dataset_mode == "prefix_copy" else "",
                "completed_at_utc": utc_now_iso(),
            }
            steps["r2"] = {"status": "completed", "completed_at_utc": r2_meta["completed_at_utc"]}

        # --- Manifest finalize（仅当 verify 步骤在 JSON 中已为 completed 后再写入，便于 Runbook 单次校验）---
        steps["verify"] = {"status": "completed", "completed_at_utc": utc_now_iso()}
        manifest = build_manifest_body(
            backup_run_id=backup_run_id,
            postgres=postgres_meta,
            r2_dataset=r2_meta,
            steps=steps,
            deployment=deployment,
            notes=cfg.notes,
        )
        manifest["run_status"] = "completed"
        mkey = _key_join(cfg.backup_prefix, "manifests", f"{backup_run_id}.json")
        raw_final = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        m_size = len(raw_final)
        m_sha = sha256_bytes(raw_final)
        client.put_object(Bucket=cfg.r2_bucket, Key=mkey, Body=raw_final, ContentType="application/json")
        head_m = client.head_object(Bucket=cfg.r2_bucket, Key=mkey)
        if int(head_m.get("ContentLength") or 0) != m_size:
            raise RuntimeError("manifest head size mismatch")
        log(f"manifest written key={mkey} size={m_size}")

        resp = client.get_object(Bucket=cfg.r2_bucket, Key=mkey)
        body = resp["Body"].read()
        if sha256_bytes(body) != m_sha:
            raise RuntimeError("manifest self-verify hash mismatch")
        loaded = json.loads(body.decode("utf-8"))
        ok, verr = validate_completed_manifest(loaded)
        if not ok:
            raise RuntimeError(f"manifest validation failed: {verr}")

        ptr = {
            "backup_run_id": backup_run_id,
            "manifest_object_key": mkey,
            "updated_at_utc": utc_now_iso(),
        }
        ptr_key = _key_join(cfg.backup_prefix, "manifests", "latest_completed.json")
        client.put_object(
            Bucket=cfg.r2_bucket,
            Key=ptr_key,
            Body=json.dumps(ptr, ensure_ascii=False, indent=2).encode("utf-8"),
            ContentType="application/json",
        )

        log(f"BACKUP COMPLETED backup_run_id={backup_run_id}")
        apply_retention(client, cfg, log)
        return 0
    except Exception as e:
        log(f"BACKUP FAILED: {e}")
        if manifest is None:
            manifest = build_manifest_body(
                backup_run_id=backup_run_id,
                postgres={},
                r2_dataset={
                    "mode": cfg.dataset_mode,
                    "prefix_or_keys": cfg.dataset_prefixes,
                    "object_count": 0,
                    "completed_at_utc": utc_now_iso(),
                },
                steps=steps,
                deployment=deployment,
                notes=cfg.notes,
            )
        manifest["run_status"] = "failed"
        manifest["failure_reason"] = str(e)[:1000]
        steps["verify"] = {"status": "failed", "error": str(e)[:500], "completed_at_utc": utc_now_iso()}
        manifest["steps"] = steps
        try:
            mk = _key_join(cfg.backup_prefix, "manifests", f"{backup_run_id}.json")
            client.put_object(
                Bucket=cfg.r2_bucket,
                Key=mk,
                Body=json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                ContentType="application/json",
            )
        except Exception as e2:
            log(f"could not write failure manifest: {e2}")
        return 1
    finally:
        try:
            os.remove(gz_path)
        except OSError:
            pass


def config_from_environ() -> BackupConfig:
    db_url = (os.environ.get("DATABASE_URL_BACKUP") or os.environ.get("DATABASE_URL") or "").strip()
    account = (os.environ.get("R2_ACCOUNT_ID") or "").strip()
    endpoint = (os.environ.get("R2_ENDPOINT_URL") or "").strip()
    if not endpoint and account:
        endpoint = f"https://{account}.r2.cloudflarestorage.com"
    prefixes = parse_dataset_prefixes(os.environ.get("R2_DATASET_PREFIXES"))
    mode = (os.environ.get("R2_DATASET_MODE") or "manifest_only").strip().lower()
    if mode not in ("manifest_only", "prefix_copy"):
        mode = "manifest_only"
    retention = int(os.environ.get("BACKUP_RETENTION_COMPLETED") or DEFAULT_RETENTION)
    drop_pct = float(os.environ.get("BACKUP_SIZE_DROP_PCT") or DEFAULT_SIZE_DROP_PCT)
    db_hint = (os.environ.get("BACKUP_DB_HOST_HINT") or "").strip()
    return BackupConfig(
        database_url=db_url,
        r2_endpoint=endpoint,
        r2_access_key=(os.environ.get("R2_ACCESS_KEY_ID") or "").strip(),
        r2_secret_key=(os.environ.get("R2_SECRET_ACCESS_KEY") or "").strip(),
        r2_bucket=(os.environ.get("R2_BUCKET_NAME") or os.environ.get("R2_BUCKET") or "").strip(),
        backup_prefix=normalize_backup_prefix(os.environ.get("BACKUP_R2_PREFIX") or DEFAULT_BACKUP_PREFIX),
        dataset_prefixes=prefixes,
        dataset_mode=mode,
        retention_completed=retention,
        size_drop_pct=drop_pct,
        deployment_env=(os.environ.get("BACKUP_DEPLOYMENT_ENV") or "prod").strip(),
        db_host_hint=db_hint,
        notes=(os.environ.get("BACKUP_NOTES") or "").strip(),
    )
