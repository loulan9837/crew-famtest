from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Any

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
CONV_JSON = os.path.join(OUTPUT_DIR, "case_conversations.json")
CONV_DB_PATH = os.path.join(OUTPUT_DIR, "case_conversations.db")
_MAX_CONVERSATIONS = 50

DEFAULT_PROJECT_ID = "FAMBASE"


def _ensure_in_allowed_dir(path: str) -> bool:
    """校验路径在 output 目录内，防御目录遍历。"""
    try:
        abs_path = os.path.abspath(path)
        abs_output = os.path.abspath(OUTPUT_DIR)
        return abs_path.startswith(abs_output)
    except Exception:
        return False


def _is_sqlite_available() -> bool:
    """检测 sqlite3 是否可用。"""
    try:
        import sqlite3 as _s  # noqa: F401
    except Exception:
        return False
    return True


def _get_conn() -> sqlite3.Connection:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    conn = sqlite3.connect(CONV_DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_tables(conn)
    return conn


def _ensure_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            title TEXT,
            run_record_id TEXT,
            prd_snapshot TEXT,
            cases_snapshot_md TEXT,
            message_count INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT,
            project_id TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS case_chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conv_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT,
            FOREIGN KEY (conv_id) REFERENCES conversations(id)
        )
        """
    )
    conn.commit()


def _load_conversations_sqlite(
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    pid = (project_id or DEFAULT_PROJECT_ID).upper()
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, title, run_record_id, message_count, created_at, updated_at, project_id
        FROM conversations
        WHERE (project_id = ? OR (project_id IS NULL AND ? = ?))
        ORDER BY updated_at DESC
        """,
        (pid, pid, DEFAULT_PROJECT_ID),
    )
    rows = cur.fetchall()
    items: list[dict[str, Any]] = []
    for r in rows:
        items.append(
            {
                "id": r["id"],
                "title": r["title"],
                "run_record_id": r["run_record_id"],
                "message_count": r["message_count"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "project_id": r["project_id"] or DEFAULT_PROJECT_ID,
            }
        )
    return items


def _load_conversations_json() -> list[dict[str, Any]]:
    if not os.path.isfile(CONV_JSON):
        return []
    try:
        with open(CONV_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []
        for r in items:
            if "project_id" not in r or not r.get("project_id"):
                r["project_id"] = DEFAULT_PROJECT_ID
        items.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        return items
    except Exception:
        return []


def _save_conversations_json(items: list[dict[str, Any]]) -> bool:
    if not _ensure_in_allowed_dir(CONV_JSON):
        return False
    tmp_path = CONV_JSON + ".tmp"
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"items": items}, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, CONV_JSON)
        return True
    except Exception:
        return False


def _generate_conv_id() -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"conv_{ts}"


def create_conversation(
    title: str,
    prd_snapshot: str,
    cases_snapshot_md: str,
    run_record_id: str = "",
    project_id: str = DEFAULT_PROJECT_ID,
) -> str | None:
    """创建新对话，返回 conv_id。超过上限时删除最旧的一条。"""
    pid = (project_id or DEFAULT_PROJECT_ID).upper()
    if _is_sqlite_available():
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(1) FROM conversations WHERE project_id = ?", (pid,))
        count = int(cur.fetchone()[0])
        if count >= _MAX_CONVERSATIONS:
            cur.execute(
                """
                SELECT id FROM conversations
                WHERE project_id = ?
                ORDER BY updated_at ASC
                LIMIT 1
                """,
                (pid,),
            )
            row = cur.fetchone()
            if row:
                cur.execute("DELETE FROM case_chat_messages WHERE conv_id = ?", (row["id"],))
                cur.execute("DELETE FROM conversations WHERE id = ?", (row["id"],))
        conv_id = _generate_conv_id()
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        cur.execute(
            """
            INSERT INTO conversations
            (id, title, run_record_id, prd_snapshot, cases_snapshot_md,
             message_count, created_at, updated_at, project_id)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (conv_id, title, run_record_id, prd_snapshot, cases_snapshot_md, 0, now, now, pid),
        )
        conn.commit()
        return conv_id
    # JSON 降级
    items = _load_conversations_json()
    if len(items) >= _MAX_CONVERSATIONS:
        items.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        items = items[: _MAX_CONVERSATIONS - 1]
    conv_id = _generate_conv_id()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    items.append(
        {
            "id": conv_id,
            "title": title,
            "run_record_id": run_record_id,
            "prd_snapshot": prd_snapshot,
            "cases_snapshot_md": cases_snapshot_md,
            "messages": [],
            "message_count": 0,
            "created_at": now,
            "updated_at": now,
            "project_id": pid,
        }
    )
    return conv_id if _save_conversations_json(items) else None


def append_message(conv_id: str, role: str, content: str) -> bool:
    """向指定对话追加一条消息。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if _is_sqlite_available():
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO case_chat_messages (conv_id, role, content, timestamp) VALUES (?,?,?,?)",
            (conv_id, role, content, ts),
        )
        cur.execute(
            "UPDATE conversations SET message_count = COALESCE(message_count,0)+1, updated_at=? WHERE id=?",
            (datetime.now().strftime("%Y-%m-%d %H:%M"), conv_id),
        )
        conn.commit()
        return True
    # JSON 降级
    items = _load_conversations_json()
    found = False
    for c in items:
        if c.get("id") == conv_id:
            msgs = c.get("messages") or []
            msgs.append({"role": role, "content": content, "timestamp": ts})
            c["messages"] = msgs
            c["message_count"] = int(c.get("message_count") or 0) + 1
            c["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            found = True
            break
    return _save_conversations_json(items) if found else False


def list_conversations(
    keyword: str = "",
    limit: int = 20,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """按关键字过滤并返回对话摘要列表。"""
    pid = (project_id or DEFAULT_PROJECT_ID).upper()
    items: list[dict[str, Any]]
    if _is_sqlite_available():
        items = _load_conversations_sqlite(project_id=pid)
    else:
        items = _load_conversations_json()
        items = [c for c in items if (c.get("project_id") or DEFAULT_PROJECT_ID).upper() == pid]
    if keyword:
        kw = keyword.strip().lower()
        items = [c for c in items if kw in (c.get("title") or "").lower()]
    return items[:limit]


def get_conversation(conv_id: str) -> dict[str, Any] | None:
    """获取完整对话记录。"""
    if _is_sqlite_available():
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, title, run_record_id, prd_snapshot, cases_snapshot_md,
                   message_count, created_at, updated_at, project_id
            FROM conversations WHERE id = ?
            """,
            (conv_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cur.execute(
            "SELECT role, content, timestamp FROM case_chat_messages WHERE conv_id = ? ORDER BY id ASC",
            (conv_id,),
        )
        msgs = [
            {"role": r["role"], "content": r["content"], "timestamp": r["timestamp"]}
            for r in cur.fetchall()
        ]
        return {
            "id": row["id"],
            "title": row["title"],
            "run_record_id": row["run_record_id"],
            "prd_snapshot": row["prd_snapshot"],
            "cases_snapshot_md": row["cases_snapshot_md"],
            "messages": msgs,
            "message_count": row["message_count"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "project_id": row["project_id"] or DEFAULT_PROJECT_ID,
        }
    items = _load_conversations_json()
    for c in items:
        if c.get("id") == conv_id:
            return c
    return None


def delete_conversation(conv_id: str) -> bool:
    """删除指定对话（主动删除途径）。"""
    if _is_sqlite_available():
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM case_chat_messages WHERE conv_id = ?", (conv_id,))
        cur.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        conn.commit()
        return True
    items = _load_conversations_json()
    items = [c for c in items if c.get("id") != conv_id]
    return _save_conversations_json(items)

