# -*- coding: utf-8 -*-
"""
全回归用例 RAG：Neon(Postgres)+pgvector 存向量，Gemini text-embedding-004 编码，
生成流水线按「当前需求文本」检索最相关若干条用例行，避免整表塞进上下文。

- 仅当 DATABASE_URL 可用且未设置 CASE_RAG_ENABLED=0 时启用检索；
- 索引在「全回归聚合内容变更」后由 app 侧调用 reindex 写入；
- 检索失败或无索引时回退为截断后的聚合正文（见 retrieve 实现）。
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Any

# Gemini text-embedding-004 默认维度（勿与模型配置不一致）
EMBEDDING_DIM = 768
EMBEDDING_MODEL = "models/text-embedding-004"
CASE_RAG_TABLE = "case_rag_chunks"
SOURCE_FULL_REGRESSION = "full_regression"
TOP_K_DEFAULT = 25
# 单条用例行写入向量库的最大字符（避免超长单行撑爆 embed 配额）
MAX_CHUNK_CHARS = 8000
# 无向量索引时回退注入的最大字符
FALLBACK_MAX_CHARS = 120_000


def _env_flag_enabled(name: str, default: bool = True) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    if not v:
        return default
    return v not in ("0", "false", "no", "off")


def is_case_rag_enabled() -> bool:
    """是否允许走向量检索路径（仍可能因缺表/无数据而回退）。"""
    if not _env_flag_enabled("CASE_RAG_ENABLED", True):
        return False
    try:
        from utils.cloud_memory import get_database_url_for_app

        return bool(get_database_url_for_app().strip())
    except Exception:
        return False


def _aggregate_content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def _normalize_project_id(project_id: str | None) -> str:
    pid = (project_id or "FAMBASE").strip().upper()
    return pid if pid in ("FAMBASE", "RM11") else "FAMBASE"


def _chunks_from_full_regression_markdown(md: str) -> list[tuple[str, str]]:
    """将全回归聚合 Markdown 拆成 (chunk_key, text) 列表。优先按 Markdown 表格行；否则按段落切分。"""
    raw = (md or "").strip()
    if not raw:
        return []

    try:
        from crew_test import _parse_markdown_tables

        tables = _parse_markdown_tables(raw)
    except Exception:
        tables = []

    out: list[tuple[str, str]] = []
    if tables:
        for ti, table in enumerate(tables):
            if not table or len(table) < 2:
                continue
            for ri, row in enumerate(table[1:], start=1):
                line = " | ".join(str(c or "").strip() for c in row)
                if not line.strip():
                    continue
                key = f"tbl{ti}_row{ri}"
                text = f"【用例行 {ti + 1}-{ri}】\n{line[:MAX_CHUNK_CHARS]}"
                out.append((key, text))
        if out:
            return out

    # 非表格：按段落切块
    parts = re.split(r"\n\s*\n+", raw)
    for i, p in enumerate(parts):
        p = p.strip()
        if len(p) < 20:
            continue
        key = f"para_{i}"
        out.append((key, p[:MAX_CHUNK_CHARS]))
    return out


def _embed_texts_google(texts: list[str], api_key: str, *, task_type: str) -> list[list[float]] | None:
    """批量调用 Gemini Embedding；失败返回 None。"""
    if not api_key or not texts:
        return None
    try:
        import google.generativeai as genai
    except ImportError:
        return None
    genai.configure(api_key=api_key.strip())
    out: list[list[float]] = []
    # 控制批量大小，避免单次请求过大
    batch_n = 16
    for i in range(0, len(texts), batch_n):
        batch = texts[i : i + batch_n]
        for t in batch:
            t = (t or "")[:MAX_CHUNK_CHARS]
            try:
                r = genai.embed_content(
                    model=EMBEDDING_MODEL,
                    content=t,
                    task_type=task_type,
                )
                emb = r.get("embedding")
                if not isinstance(emb, list) or len(emb) != EMBEDDING_DIM:
                    return None
                out.append(emb)
            except Exception:
                return None
    return out


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


def ensure_case_rag_schema(cur: Any) -> None:
    """创建 pgvector 扩展与 case_rag_chunks 表（幂等）。"""
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {CASE_RAG_TABLE} (
            id BIGSERIAL PRIMARY KEY,
            project_id VARCHAR(32) NOT NULL,
            source_kind VARCHAR(32) NOT NULL DEFAULT '{SOURCE_FULL_REGRESSION}',
            chunk_key VARCHAR(256) NOT NULL,
            content TEXT NOT NULL,
            embedding vector({EMBEDDING_DIM}) NOT NULL,
            content_hash VARCHAR(64) NOT NULL DEFAULT '',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (project_id, source_kind, chunk_key)
        );
        """
    )
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS idx_case_rag_proj_kind ON {CASE_RAG_TABLE} (project_id, source_kind);"
    )


def delete_chunks_for_project(cur: Any, project_id: str, source_kind: str = SOURCE_FULL_REGRESSION) -> None:
    pid = _normalize_project_id(project_id)
    cur.execute(
        f"DELETE FROM {CASE_RAG_TABLE} WHERE project_id = %s AND source_kind = %s",
        (pid, source_kind),
    )


def reindex_full_regression_from_markdown(
    project_id: str,
    markdown: str,
    gemini_api_key: str,
) -> tuple[bool, str]:
    """
    用当前全回归聚合正文重建向量索引（先删后插）。
    返回 (ok, message)。
    """
    if not is_case_rag_enabled():
        return False, "未启用或缺少数据库连接"
    key = (gemini_api_key or os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key:
        return False, "缺少 GEMINI API Key，无法向量化"

    from utils.cloud_memory import get_database_url_for_app

    url = get_database_url_for_app().strip()
    if not url:
        return False, "缺少 DATABASE_URL"

    chunks = _chunks_from_full_regression_markdown(markdown)
    if not chunks:
        return True, "无可用用例行，已清空旧索引"
    texts = [c[1] for c in chunks]
    keys = [c[0] for c in chunks]
    vecs = _embed_texts_google(texts, key, task_type="retrieval_document")
    if vecs is None or len(vecs) != len(chunks):
        return False, "向量化失败（请检查 google-generativeai 与模型可用性）"

    agg_hash = _aggregate_content_hash(markdown)

    try:
        import psycopg2  # type: ignore[import-untyped]
    except ImportError as e:
        return False, f"缺少 psycopg2: {e}"

    conn = None
    try:
        conn = psycopg2.connect(url)
        try:
            from pgvector.psycopg2 import register_vector

            register_vector(conn)
        except Exception:
            pass
        cur = conn.cursor()
        ensure_case_rag_schema(cur)
        pid = _normalize_project_id(project_id)
        delete_chunks_for_project(cur, pid, SOURCE_FULL_REGRESSION)
        for ck, content, emb in zip(keys, texts, vecs, strict=True):
            cur.execute(
                f"""
                INSERT INTO {CASE_RAG_TABLE}
                  (project_id, source_kind, chunk_key, content, embedding, content_hash)
                VALUES (%s, %s, %s, %s, %s::vector, %s)
                """,
                (pid, SOURCE_FULL_REGRESSION, ck[:250], content, _vector_literal(emb), agg_hash),
            )
        conn.commit()
        cur.close()
        return True, f"已索引 {len(chunks)} 条用例行向量"
    except Exception as e:
        if conn:
            conn.rollback()
        return False, str(e)
    finally:
        if conn:
            conn.close()


def retrieve_relevant_case_context(
    project_id: str,
    query: str,
    aggregate_markdown_fallback: str,
    gemini_api_key: str | None,
    *,
    top_k: int = TOP_K_DEFAULT,
) -> str:
    """
    按需求 query 检索最相关用例行，拼成注入串；失败或无数据时用 aggregate 截断回退。
    """
    q = (query or "").strip()
    fb = (aggregate_markdown_fallback or "").strip()
    if not fb:
        return ""

    if not is_case_rag_enabled() or not q:
        return _format_fallback_block(fb)

    key = (gemini_api_key or os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key:
        return _format_fallback_block(fb)

    qv = _embed_texts_google([q[:MAX_CHUNK_CHARS]], key, task_type="retrieval_query")
    if not qv or len(qv) != 1:
        return _format_fallback_block(fb)

    from utils.cloud_memory import get_database_url_for_app

    url = get_database_url_for_app().strip()
    if not url:
        return _format_fallback_block(fb)

    try:
        import psycopg2  # type: ignore[import-untyped]
    except ImportError:
        return _format_fallback_block(fb)

    pid = _normalize_project_id(project_id)
    vec_lit = _vector_literal(qv[0])
    conn = None
    try:
        conn = psycopg2.connect(url)
        try:
            from pgvector.psycopg2 import register_vector

            register_vector(conn)
        except Exception:
            pass
        cur = conn.cursor()
        ensure_case_rag_schema(cur)
        cur.execute(
            f"""
            SELECT content FROM {CASE_RAG_TABLE}
            WHERE project_id = %s AND source_kind = %s
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (pid, SOURCE_FULL_REGRESSION, vec_lit, max(1, min(int(top_k), 50))),
        )
        rows = cur.fetchall() or []
        cur.close()
        if not rows:
            return _format_fallback_block(fb)
        parts = [str(r[0] or "").strip() for r in rows if r and r[0]]
        body = "\n\n---\n\n".join(parts)
        return (
            "【全回归测试用例（语义检索，与当前需求最相关的 "
            + str(len(parts))
            + " 条；完整表请在本机项目记忆或导出 Excel 查看）】\n\n"
            + body
        )
    except Exception:
        return _format_fallback_block(fb)
    finally:
        if conn:
            conn.close()


def _format_fallback_block(fb: str) -> str:
    if len(fb) <= FALLBACK_MAX_CHARS:
        return "【全回归测试用例（聚合，向量检索不可用或未命中索引时回退）】\n\n" + fb
    return (
        "【全回归测试用例（聚合，已截断；建议配置 Neon+pgvector 并重新导入以启用语义检索）】\n\n"
        + fb[:FALLBACK_MAX_CHARS]
        + "\n\n...(已截断)"
    )
