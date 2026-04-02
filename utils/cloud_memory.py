# -*- coding: utf-8 -*-
"""
云端项目记忆：R2 存原始字节 + Neon/Postgres 存元数据与解析文本。
与本地 memory_store（Sqlite）并行；仅当环境变量或 st.secrets 配置齐全时启用。

应用写入请使用 DATABASE_URL / NEON_DATABASE_URL / DATABASE_URL_APP，
勿使用 DATABASE_URL_BACKUP（该串用于冷备 pg_dump，见备份方案文档）。
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from typing import Any

# 与用户需求一致的表名（仅存在于远程 Postgres，与本地 Sqlite 无关）
PROJECT_MEMORY_TABLE = "project_memory"
ALLOWED_CLOUD_PROJECT_IDS = frozenset({"FAMBASE", "RM11"})
MAX_FILE_NAME_DB_LEN = 512
MAX_SOURCE_TYPE_LEN = 64
MAX_R2_PREFIX_LEN = 128


def get_cloud_secret(key: str) -> str:
    """优先 os.environ，其次 Streamlit secrets（支持平铺键或 [cloud_memory] 小节）。"""
    v = (os.environ.get(key) or "").strip()
    if v:
        return v
    try:
        import streamlit as st

        if not hasattr(st, "secrets"):
            return ""
        sec = st.secrets
        if key in sec:
            return str(sec[key]).strip()
        for block in ("cloud_memory", "CLOUD_MEMORY"):
            cm = sec.get(block)
            if isinstance(cm, dict) and key in cm:
                return str(cm[key]).strip()
    except Exception:
        pass
    return ""


def get_database_url_for_app() -> str:
    """应用侧读写 Neon/Postgres 的连接串（非冷备专用串）。"""
    for k in ("NEON_DATABASE_URL", "DATABASE_URL", "DATABASE_URL_APP"):
        u = (os.environ.get(k) or "").strip()
        if u:
            return u
    try:
        import streamlit as st

        if hasattr(st, "secrets"):
            sec = st.secrets
            for key in ("NEON_DATABASE_URL", "DATABASE_URL", "DATABASE_URL_APP"):
                if key in sec:
                    return str(sec[key]).strip()
            cm = sec.get("cloud_memory") or sec.get("CLOUD_MEMORY")
            if isinstance(cm, dict):
                for key in ("NEON_DATABASE_URL", "DATABASE_URL", "DATABASE_URL_APP"):
                    if key in cm:
                        return str(cm[key]).strip()
    except Exception:
        pass
    return ""


def is_cloud_memory_configured() -> bool:
    if not get_database_url_for_app():
        return False
    acc = get_cloud_secret("R2_ACCOUNT_ID")
    ep = get_cloud_secret("R2_ENDPOINT_URL")
    if not acc and not ep:
        return False
    if not get_cloud_secret("R2_ACCESS_KEY_ID") or not get_cloud_secret("R2_SECRET_ACCESS_KEY"):
        return False
    if not (get_cloud_secret("R2_BUCKET_NAME") or get_cloud_secret("R2_BUCKET")):
        return False
    return True


def _r2_endpoint() -> str:
    ep = get_cloud_secret("R2_ENDPOINT_URL")
    if ep:
        return ep.rstrip("/")
    acc = get_cloud_secret("R2_ACCOUNT_ID")
    if acc:
        return f"https://{acc}.r2.cloudflarestorage.com"
    return ""


def _r2_bucket() -> str:
    return get_cloud_secret("R2_BUCKET_NAME") or get_cloud_secret("R2_BUCKET")


def _s3_client():
    import boto3  # type: ignore[import-untyped]
    from botocore.config import Config  # type: ignore[import-untyped]

    return boto3.client(
        "s3",
        endpoint_url=_r2_endpoint(),
        aws_access_key_id=get_cloud_secret("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=get_cloud_secret("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def _normalize_cloud_project_id(project_id: str | None) -> str:
    pid = (project_id or "FAMBASE").upper()
    return pid if pid in ALLOWED_CLOUD_PROJECT_IDS else "FAMBASE"


def _normalize_r2_key_prefix(prefix: str) -> str:
    """禁止 ..、绝对路径与异常字符，避免 Key 注入。"""
    p = (prefix or "uploads").strip().strip("/")
    if not p or ".." in p or "//" in p or p.startswith(("/", "\\")):
        return "uploads"
    if not re.match(r"^[a-zA-Z0-9_\-/]+$", p):
        return "uploads"
    return p[:MAX_R2_PREFIX_LEN].strip("/") or "uploads"


def _safe_file_component(name: str, max_len: int = 180) -> str:
    base = os.path.basename((name or "upload").strip()) or "upload"
    base = re.sub(r"[^\w.\-()\u4e00-\u9fff]+", "_", base)
    return base[:max_len] if len(base) > max_len else base


def _sanitize_source_type(source_type: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_\-]", "", (source_type or "upload").strip())[:MAX_SOURCE_TYPE_LEN]
    return s or "upload"


def upload_to_r2(file_bytes: bytes, file_name: str, *, project_id: str, prefix: str = "uploads") -> str:
    """
    上传至 R2，返回对象 Key（非 URL）。
    Key 形如：uploads/FAMBASE/{uuid}_{filename}
    """
    if not file_bytes:
        raise ValueError("file_bytes 为空")
    pid = _normalize_cloud_project_id(project_id)
    pfx = _normalize_r2_key_prefix(prefix)
    safe = _safe_file_component(file_name)
    uid = uuid.uuid4().hex[:12]
    key = f"{pfx}/{pid}/{uid}_{safe}"
    client = _s3_client()
    bucket = _r2_bucket()
    client.put_object(Bucket=bucket, Key=key, Body=file_bytes)
    return key


def _ensure_table(cur) -> None:
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PROJECT_MEMORY_TABLE} (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            project_id VARCHAR(16) NOT NULL,
            file_name TEXT NOT NULL,
            r2_object_key TEXT NOT NULL,
            raw_content TEXT NOT NULL DEFAULT '',
            source_type VARCHAR(64) NOT NULL DEFAULT 'upload',
            content_hash VARCHAR(64) NOT NULL DEFAULT ''
        );
        """
    )
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS idx_pm_project_created ON {PROJECT_MEMORY_TABLE} (project_id, created_at DESC);"
    )
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS idx_pm_project_hash ON {PROJECT_MEMORY_TABLE} (project_id, content_hash);"
    )


def _content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def cloud_row_exists(project_id: str, content_hash: str) -> bool:
    """同一项目下是否已有相同解析内容（防重复写入云端）。"""
    if not content_hash or not is_cloud_memory_configured():
        return False
    try:
        import psycopg2  # type: ignore[import-untyped]
    except ImportError:
        return False
    url = get_database_url_for_app()
    conn = None
    try:
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        _ensure_table(cur)
        cur.execute(
            f"SELECT 1 FROM {PROJECT_MEMORY_TABLE} WHERE project_id = %s AND content_hash = %s LIMIT 1",
            (_normalize_cloud_project_id(project_id), content_hash),
        )
        row = cur.fetchone()
        cur.close()
        return bool(row)
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def save_to_neon(
    file_name: str,
    r2_object_key: str,
    content: str,
    *,
    project_id: str,
    source_type: str = "design_mockup",
) -> int:
    """
    写入 project_memory 表，返回新行 id。
    """
    import psycopg2  # type: ignore[import-untyped]

    url = get_database_url_for_app()
    if not url:
        raise RuntimeError("未配置数据库连接串（NEON_DATABASE_URL / DATABASE_URL / DATABASE_URL_APP）")
    ch = _content_hash(content)
    pid = _normalize_cloud_project_id(project_id)
    fn = (file_name or "")[:MAX_FILE_NAME_DB_LEN]
    stype = _sanitize_source_type(source_type)
    conn = psycopg2.connect(url)
    try:
        cur = conn.cursor()
        _ensure_table(cur)
        cur.execute(
            f"""
            INSERT INTO {PROJECT_MEMORY_TABLE}
                (file_name, r2_object_key, raw_content, project_id, source_type, content_hash)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (fn, r2_object_key, content or "", pid, stype, ch),
        )
        row = cur.fetchone()
        new_id = int(row[0]) if row else 0
        conn.commit()
        cur.close()
        return new_id
    finally:
        conn.close()


def load_recent_history_from_neon(limit: int = 5, project_id: str | None = None) -> list[dict[str, Any]]:
    """拉取当前项目最近若干条云端记忆（按 created_at DESC）。"""
    try:
        import psycopg2  # type: ignore[import-untyped]
    except ImportError:
        return []
    if not is_cloud_memory_configured():
        return []
    url = get_database_url_for_app()
    pid = _normalize_cloud_project_id(project_id)
    lim = max(1, min(int(limit), 50))
    conn = None
    try:
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        _ensure_table(cur)
        cur.execute(
            f"""
            SELECT id, created_at, file_name, r2_object_key, raw_content, source_type
            FROM {PROJECT_MEMORY_TABLE}
            WHERE project_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (pid, lim),
        )
        rows = cur.fetchall() or []
        cur.close()
        out: list[dict[str, Any]] = []
        for r in rows:
            created = r[1]
            ts = created.isoformat() if hasattr(created, "isoformat") else str(created)
            out.append(
                {
                    "id": int(r[0]),
                    "created_at": ts,
                    "file_name": r[2] or "",
                    "r2_object_key": r[3] or "",
                    "raw_content": r[4] or "",
                    "source_type": r[5] or "",
                }
            )
        return out
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def sync_design_file_to_cloud(
    *,
    file_name: str,
    raw_bytes: bytes,
    parsed_content: str,
    project_id: str,
    skip_if_duplicate: bool = True,
) -> tuple[bool, str]:
    """
    设计图等：上传原始文件到 R2，解析文本写入 Neon。
    返回 (ok, message)。
    """
    if not is_cloud_memory_configured():
        return False, "未配置云端记忆（数据库或R2）"
    try:
        import boto3  # noqa: F401
        import psycopg2  # noqa: F401
    except ImportError as e:
        return False, f"缺少依赖: {e}"
    ch = _content_hash(parsed_content)
    if skip_if_duplicate and cloud_row_exists(project_id, ch):
        return True, "云端已存在相同解析内容，已跳过重复写入"
    key = upload_to_r2(raw_bytes, file_name, project_id=project_id)
    try:
        save_to_neon(file_name, key, parsed_content, project_id=project_id, source_type="design_mockup")
    except Exception as e:
        try:
            cl = _s3_client()
            cl.delete_object(Bucket=_r2_bucket(), Key=key)
        except Exception:
            pass
        return False, f"数据库写入失败，已尝试删除已上传的R2对象: {e}"
    return True, "已同步至云端（R2+数据库）"


def sync_text_demand_to_cloud(
    *,
    file_name: str,
    content: str,
    project_id: str,
    source_type: str = "manual",
    skip_if_duplicate: bool = True,
) -> tuple[bool, str]:
    """无二进制文件时：仅将文本写入数据库，r2_object_key 记空占位。"""
    if not is_cloud_memory_configured():
        return False, "未配置云端记忆"
    try:
        import psycopg2  # noqa: F401
    except ImportError as e:
        return False, f"缺少依赖: {e}"
    ch = _content_hash(content)
    if skip_if_duplicate and cloud_row_exists(project_id, ch):
        return True, "云端已存在相同内容，已跳过"
    save_to_neon(file_name, "", content, project_id=project_id, source_type=source_type)
    return True, "已写入云端数据库"
