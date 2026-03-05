# -*- coding: utf-8 -*-
"""
项目记忆存储：干净的需求文档历史，供 Agent 与用户查阅
- 需求文档: manual（粘贴导入）
- 运行产出: run_summary（可选供 Agent）

设计目标（见《项目记忆-重新规划实施方案》）：
- 存储后端可插拔：sqlite3 首选，缺失时自动降级到 JSON 文件后端；
- 惰性初始化：模块 import 时不做 IO，首次读写时才建立连接/读取文件；
- memory_store 自身不抛 ImportError，对外接口始终可用。
"""
from __future__ import annotations

import json
import os
import hashlib
import threading
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
MEMORY_DB_PATH = os.path.join(CONFIG_DIR, "memory.db")
JSON_PATH = os.path.join(CONFIG_DIR, "memory_entries.json")

# 视为「需求文档」的 source_type，供 Agent 使用
DEMAND_SOURCE_TYPES = ("manual", "design_mockup")
# 测试用例：导入后供 Agent 理解项目既有用例
TEST_CASES_SOURCE_TYPE = "test_cases"
# 设计图（Gemini Vision 解析后的文本描述）
DESIGN_MOCKUP_SOURCE_TYPE = "design_mockup"

# 项目标识：当前版本支持 Fambase / RM11 双项目
DEFAULT_PROJECT_ID = "FAMBASE"

_SQLITE_AVAILABLE = False
try:
    import sqlite3  # type: ignore

    _SQLITE_AVAILABLE = True
except Exception:  # pragma: no cover - 仅在缺 sqlite3 时触发
    sqlite3 = None  # type: ignore[assignment]
    _SQLITE_AVAILABLE = False


def _compute_content_hash(content: str) -> str:
    """计算内容 SHA-256 哈希，用于去重。"""
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now().isoformat()


@runtime_checkable
class _MemoryBackend(Protocol):
    def add_entry(
        self,
        source_type: str,
        content: str,
        source_id: str = "",
        title: str = "",
        summary: str = "",
        project_id: str | None = None,
    ) -> int: ...

    def add_entry_with_dedup(
        self,
        source_type: str,
        content: str,
        source_id: str = "",
        title: str = "",
        summary: str = "",
        project_id: str | None = None,
    ) -> tuple[int, str]: ...

    def search(self, keyword: str, limit: int = 50, project_id: str | None = None) -> list[dict[str, Any]]: ...

    def delete_entry(self, entry_id: int) -> bool: ...

    def list_recent(self, limit: int = 50, project_id: str | None = None) -> list[dict[str, Any]]: ...

    def update_agent_summary(self, entry_id: int, summary: str, status: str) -> bool: ...

    def list_import_history(self, limit: int = 20, project_id: str | None = None) -> list[dict[str, Any]]: ...

    def get_recent_for_agent(
        self,
        limit: int = 10,
        max_content_len: int = 3000,
        demand_only: bool = True,
        include_test_cases: bool = False,
        project_id: str | None = None,
    ) -> str: ...

    def get_all_demands_full_for_chat(
        self,
        limit: int = 30,
        max_total_chars: int = 80000,
        include_test_cases: bool = True,
        project_id: str | None = None,
    ) -> str: ...

    def get_entry_content(self, source_type: str, source_id: str, project_id: str | None = None) -> str | None: ...

    def list_for_browse(
        self,
        source_type_filter: str = "",
        limit: int = 50,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]: ...


class SqliteBackend:
    """sqlite3 可用时的后端实现。"""

    def __init__(self) -> None:
        # Streamlit Cloud 可能在多线程下复用同一 backend 实例：
        # sqlite 连接必须与创建它的线程一致，故按线程隔离连接。
        self._local = threading.local()
        self._tables_ready = False
        self._init_lock = threading.Lock()

    def _get_conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            conn = sqlite3.connect(MEMORY_DB_PATH)  # type: ignore[call-arg]
            conn.row_factory = sqlite3.Row  # type: ignore[attr-defined]
            self._local.conn = conn
        if not self._tables_ready:
            with self._init_lock:
                if not self._tables_ready:
                    self._ensure_tables(conn)
                    self._tables_ready = True
        return conn

    def _ensure_tables(self, conn) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_id TEXT,
                title TEXT,
                content TEXT NOT NULL,
                summary TEXT
            )
        """
        )
        for col in ("agent_summary", "agent_summary_status"):
            try:
                conn.execute(f"ALTER TABLE memory_entries ADD COLUMN {col} TEXT")
                conn.commit()
            except Exception:
                pass
        try:
            conn.execute("ALTER TABLE memory_entries ADD COLUMN content_hash TEXT")
            conn.commit()
        except Exception:
            pass
        # project_id：用于双项目工作台的项目隔离；存量数据默认视为 FAMBASE
        try:
            conn.execute("ALTER TABLE memory_entries ADD COLUMN project_id TEXT")
            conn.commit()
        except Exception:
            # 已存在列时静默忽略
            pass
        try:
            conn.execute(
                "UPDATE memory_entries SET project_id=? WHERE project_id IS NULL OR project_id = ''",
                (DEFAULT_PROJECT_ID,),
            )
            conn.commit()
        except Exception:
            # 迁移失败不影响读写，只是会将缺失 project_id 的旧记录视为默认项目
            pass
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_index (
                content_hash TEXT PRIMARY KEY,
                entry_id INTEGER NOT NULL,
                file_name TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_created ON memory_entries(created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_source ON memory_entries(source_type)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_content_hash ON memory_entries(content_hash)"
        )

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        d = dict(row)
        d.setdefault("agent_summary", "")
        d.setdefault("agent_summary_status", "pending")
        d.setdefault("content_hash", "")
        d.setdefault("project_id", DEFAULT_PROJECT_ID)
        return d

    # 公共接口实现 ---------------------------------------------------------

    def add_entry(
        self,
        source_type: str,
        content: str,
        source_id: str = "",
        title: str = "",
        summary: str = "",
        project_id: str | None = None,
    ) -> int:
        pid = (project_id or DEFAULT_PROJECT_ID).upper()
        conn = self._get_conn()
        now = _now_iso()
        sid = (source_id or "").strip()
        content_hash = _compute_content_hash(content)
        if sid:
            existing = conn.execute(
                "SELECT id FROM memory_entries WHERE source_type = ? AND source_id = ? AND project_id = ?",
                (source_type, sid, pid),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE memory_entries SET created_at=?, title=?, content=?, summary=?,
                       content_hash=?, project_id=?, agent_summary='', agent_summary_status='pending'
                       WHERE source_type=? AND source_id=? AND project_id=?""",
                    (now, title or "", content, summary or "", content_hash, pid, source_type, sid, pid),
                )
                conn.execute(
                    """INSERT INTO memory_index (content_hash, entry_id, file_name, created_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(content_hash) DO UPDATE SET
                         entry_id=excluded.entry_id, file_name=excluded.file_name, created_at=excluded.created_at""",
                    (content_hash, int(existing["id"]), title or "", now),
                )
                conn.commit()
                return int(existing["id"])
        cur = conn.execute(
            """INSERT INTO memory_entries
               (created_at, source_type, source_id, title, content, summary, content_hash, project_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (now, source_type, sid, title or "", content, summary or "", content_hash, pid),
        )
        conn.commit()
        rowid = int(cur.lastrowid or 0)
        if rowid:
            conn.execute(
                """INSERT OR REPLACE INTO memory_index (content_hash, entry_id, file_name, created_at)
                   VALUES (?, ?, ?, ?)""",
                (content_hash, rowid, title or "", now),
            )
            conn.commit()
        return rowid

    def add_entry_with_dedup(
        self,
        source_type: str,
        content: str,
        source_id: str = "",
        title: str = "",
        summary: str = "",
        project_id: str | None = None,
    ) -> tuple[int, str]:
        clean_content = (content or "").strip()
        if not clean_content:
            return 0, "skipped"
        conn = self._get_conn()
        now = _now_iso()
        sid = (source_id or "").strip()
        pid = (project_id or DEFAULT_PROJECT_ID).upper()
        content_hash = _compute_content_hash(clean_content)
        existing_by_hash = conn.execute(
            "SELECT id FROM memory_entries WHERE content_hash = ? AND project_id = ? LIMIT 1",
            (content_hash, pid),
        ).fetchone()
        if existing_by_hash:
            return int(existing_by_hash["id"]), "skipped"
        if sid:
            existing = conn.execute(
                "SELECT id FROM memory_entries WHERE source_type = ? AND source_id = ? AND project_id = ?",
                (source_type, sid, pid),
            ).fetchone()
            if existing:
                rowid = int(existing["id"])
                conn.execute(
                    """UPDATE memory_entries
                       SET created_at=?, title=?, content=?, summary=?, content_hash=?, project_id=?,
                           agent_summary='', agent_summary_status='pending'
                       WHERE id=?""",
                    (now, title or "", clean_content, summary or "", content_hash, pid, rowid),
                )
                conn.execute(
                    """INSERT INTO memory_index (content_hash, entry_id, file_name, created_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(content_hash) DO UPDATE SET
                         entry_id=excluded.entry_id, file_name=excluded.file_name, created_at=excluded.created_at""",
                    (content_hash, rowid, title or "", now),
                )
                conn.commit()
                return rowid, "updated"
        cur = conn.execute(
            """INSERT INTO memory_entries
               (created_at, source_type, source_id, title, content, summary, content_hash, project_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (now, source_type, sid, title or "", clean_content, summary or "", content_hash, pid),
        )
        rowid = int(cur.lastrowid or 0)
        if rowid:
            conn.execute(
                """INSERT OR REPLACE INTO memory_index (content_hash, entry_id, file_name, created_at)
                   VALUES (?, ?, ?, ?)""",
                (content_hash, rowid, title or "", now),
            )
            conn.commit()
        return rowid, "added"

    def search(self, keyword: str, limit: int = 50, project_id: str | None = None) -> list[dict[str, Any]]:
        if not keyword or not keyword.strip():
            return self.list_recent(limit=limit, project_id=project_id)
        conn = self._get_conn()
        kw = keyword.strip()
        terms = [t.strip() for t in kw.split() if t.strip()]
        if not terms:
            return self.list_recent(limit=limit, project_id=project_id)
        pid = (project_id or DEFAULT_PROJECT_ID).upper()
        where_clause = " AND ".join(
            [f"(content LIKE ? OR summary LIKE ? OR title LIKE ?)" for _ in terms]
        )
        args_where: list[str] = []
        for t in terms:
            p = f"%{t}%"
            args_where.extend([p, p, p])
        rows = conn.execute(
            f"""SELECT id, created_at, source_type, source_id, title, content, summary, agent_summary,
                        agent_summary_status, content_hash, project_id
                FROM memory_entries
                WHERE ({where_clause}) AND project_id = ?
                ORDER BY created_at DESC
                LIMIT ?""",
            (*args_where, pid, limit * 3),
        ).fetchall()
        entries = [self._row_to_dict(r) for r in rows]
        if not entries:
            return []

        def score(e: dict[str, Any]) -> int:
            text = f"{e.get('title','')} {e.get('summary','')} {e.get('content','')}"
            return sum(1 for t in terms if t in text)

        entries.sort(key=lambda e: (score(e), e.get("created_at", "")), reverse=True)
        return entries[:limit]

    def delete_entry(self, entry_id: int) -> bool:
        conn = self._get_conn()
        conn.execute("DELETE FROM memory_index WHERE entry_id = ?", (entry_id,))
        cur = conn.execute("DELETE FROM memory_entries WHERE id = ?", (entry_id,))
        conn.commit()
        return cur.rowcount > 0

    def list_recent(self, limit: int = 50, project_id: str | None = None) -> list[dict[str, Any]]:
        conn = self._get_conn()
        pid = (project_id or DEFAULT_PROJECT_ID).upper()
        rows = conn.execute(
            """SELECT id, created_at, source_type, source_id, title, content, summary,
                      agent_summary, agent_summary_status, content_hash, project_id
               FROM memory_entries
               WHERE project_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (pid, limit),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def update_agent_summary(self, entry_id: int, summary: str, status: str) -> bool:
        conn = self._get_conn()
        cur = conn.execute(
            "UPDATE memory_entries SET agent_summary=?, agent_summary_status=? WHERE id=?",
            (summary or "", status, entry_id),
        )
        conn.commit()
        return cur.rowcount > 0

    def list_import_history(self, limit: int = 20, project_id: str | None = None) -> list[dict[str, Any]]:
        conn = self._get_conn()
        pid = (project_id or DEFAULT_PROJECT_ID).upper()
        try:
            rows = conn.execute(
                """SELECT id, created_at, source_type, source_id, title, content, summary,
                          COALESCE(agent_summary,'') as agent_summary,
                          COALESCE(agent_summary_status,'pending') as agent_summary_status,
                          COALESCE(content_hash,'') as content_hash,
                          COALESCE(project_id, ?) as project_id
                   FROM memory_entries
                   WHERE COALESCE(project_id, ?) = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (pid, pid, pid, limit),
            ).fetchall()
        except Exception:
            rows = conn.execute(
                """SELECT id, created_at, source_type, source_id, title, content, summary,
                          '' as agent_summary, 'pending' as agent_summary_status,
                          COALESCE(content_hash,'') as content_hash,
                          COALESCE(project_id, ?) as project_id
                   FROM memory_entries
                   WHERE COALESCE(project_id, ?) = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (pid, pid, pid, limit),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_recent_for_agent(
        self,
        limit: int = 10,
        max_content_len: int = 3000,
        demand_only: bool = True,
        include_test_cases: bool = False,
        project_id: str | None = None,
    ) -> str:
        conn = self._get_conn()
        pid = (project_id or DEFAULT_PROJECT_ID).upper()
        if demand_only and not include_test_cases:
            types = DEMAND_SOURCE_TYPES
        elif include_test_cases:
            types = (*DEMAND_SOURCE_TYPES, TEST_CASES_SOURCE_TYPE)
        else:
            types = None
        if types:
            placeholders = ",".join(["?"] * len(types))
            rows = conn.execute(
                f"""SELECT id, created_at, source_type, source_id, title, content, summary,
                          agent_summary, agent_summary_status, content_hash, project_id
                    FROM memory_entries
                    WHERE source_type IN ({placeholders}) AND project_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?""",
                (*types, pid, limit * 2),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, created_at, source_type, source_id, title, content, summary,
                          agent_summary, agent_summary_status, content_hash, project_id
                   FROM memory_entries
                   WHERE project_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (pid, limit * 2,),
            ).fetchall()
        entries = [self._row_to_dict(r) for r in rows]
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for e in entries:
            sid = (e.get("source_id") or "").strip()
            key = f"{e.get('source_type')}:{sid}" if sid else f"_{e.get('id')}"
            if key not in seen:
                seen.add(key)
                unique.append(e)
            if len(unique) >= limit:
                break
        if not unique:
            return ""
        parts: list[str] = []
        per_len = max(200, max_content_len // len(unique))
        for e in unique:
            title = (e.get("title") or "").strip() or f"[{e.get('source_type')}]"
            summary = (e.get("summary") or e.get("content", "")).strip()
            if len(summary) > per_len:
                summary = summary[:per_len] + "..."
            parts.append(f"【{title}】{e.get('created_at', '')}\n{summary}")
        return "\n\n---\n\n".join(parts)

    def get_all_demands_full_for_chat(
        self,
        limit: int = 30,
        max_total_chars: int = 80000,
        include_test_cases: bool = True,
        project_id: str | None = None,
    ) -> str:
        conn = self._get_conn()
        pid = (project_id or DEFAULT_PROJECT_ID).upper()
        types = (*DEMAND_SOURCE_TYPES, TEST_CASES_SOURCE_TYPE) if include_test_cases else DEMAND_SOURCE_TYPES
        placeholders = ",".join(["?"] * len(types))
        rows = conn.execute(
            f"""SELECT id, created_at, source_type, source_id, title, content, summary,
                      agent_summary, agent_summary_status, content_hash, project_id
                FROM memory_entries
                WHERE source_type IN ({placeholders}) AND project_id = ?
                ORDER BY created_at DESC
                LIMIT ?""",
            (*types, pid, limit * 2),
        ).fetchall()
        entries = [self._row_to_dict(r) for r in rows]
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for e in entries:
            sid = (e.get("source_id") or "").strip()
            key = f"{e.get('source_type')}:{sid}" if sid else f"_{e.get('id')}"
            if key not in seen:
                seen.add(key)
                unique.append(e)
            if len(unique) >= limit:
                break
        if not unique:
            return ""
        parts: list[str] = []
        total = 0
        for e in unique:
            title = (e.get("title") or "").strip() or f"[{e.get('source_type')}]"
            content = (e.get("content") or e.get("summary") or "").strip()
            block = f"【{title}】{e.get('created_at', '')}\n\n{content}"
            if total + len(block) > max_total_chars:
                block = block[: max_total_chars - total - 20] + "\n...(已截断)"
            parts.append(block)
            total += len(block)
            if total >= max_total_chars:
                break
        return "\n\n---\n\n".join(parts)

    def get_entry_content(self, source_type: str, source_id: str, project_id: str | None = None) -> str | None:
        conn = self._get_conn()
        pid = (project_id or DEFAULT_PROJECT_ID).upper()
        row = conn.execute(
            "SELECT content FROM memory_entries WHERE source_type = ? AND source_id = ? AND project_id = ?",
            (source_type, source_id, pid),
        ).fetchone()
        return row["content"] if row else None

    def list_for_browse(
        self,
        source_type_filter: str = "",
        limit: int = 50,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        conn = self._get_conn()
        pid = (project_id or DEFAULT_PROJECT_ID).upper()
        if source_type_filter and source_type_filter.strip():
            rows = conn.execute(
                """SELECT id, created_at, source_type, source_id, title, content, summary,
                          agent_summary, agent_summary_status, content_hash, project_id
                   FROM memory_entries
                   WHERE source_type = ? AND project_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (source_type_filter.strip(), pid, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, created_at, source_type, source_id, title, content, summary,
                          agent_summary, agent_summary_status, content_hash, project_id
                   FROM memory_entries
                   WHERE project_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (pid, limit),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]


class JsonFileBackend:
    """sqlite3 不可用时的 fallback 后端，基于 JSON 文件。

    适用于云端缺 sqlite3 的环境，功能与 SqliteBackend 尽量保持一致。
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        if os.path.isfile(JSON_PATH):
            try:
                with open(JSON_PATH, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {"entries": [], "next_id": 1}
        else:
            self._data = {"entries": [], "next_id": 1}
        if "entries" not in self._data or "next_id" not in self._data:
            self._data = {"entries": [], "next_id": 1}
        return self._data

    def _save(self) -> None:
        if self._data is None:
            return
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def _row_to_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        d = dict(row)
        d.setdefault("agent_summary", "")
        d.setdefault("agent_summary_status", "pending")
        d.setdefault("content_hash", "")
        d.setdefault("project_id", DEFAULT_PROJECT_ID)
        return d

    # 公共接口实现 ---------------------------------------------------------

    def add_entry(
        self,
        source_type: str,
        content: str,
        source_id: str = "",
        title: str = "",
        summary: str = "",
        project_id: str | None = None,
    ) -> int:
        data = self._load()
        entries: list[dict[str, Any]] = data.get("entries", [])
        now = _now_iso()
        sid = (source_id or "").strip()
        content_hash = _compute_content_hash(content)
        pid = (project_id or DEFAULT_PROJECT_ID).upper()
        if sid:
            for e in entries:
                if (
                    e.get("source_type") == source_type
                    and (e.get("source_id") or "") == sid
                    and (e.get("project_id") or DEFAULT_PROJECT_ID).upper() == pid
                ):
                    e.update(
                        {
                            "created_at": now,
                            "title": title or "",
                            "content": content,
                            "summary": summary or "",
                            "content_hash": content_hash,
                            "agent_summary": "",
                            "agent_summary_status": "pending",
                            "project_id": pid,
                        }
                    )
                    self._save()
                    return int(e.get("id"))
        new_id = int(data.get("next_id", 1))
        entry = {
            "id": new_id,
            "created_at": now,
            "source_type": source_type,
            "source_id": sid,
            "title": title or "",
            "content": content,
            "summary": summary or "",
            "content_hash": content_hash,
            "agent_summary": "",
            "agent_summary_status": "pending",
            "project_id": pid,
        }
        entries.append(entry)
        data["next_id"] = new_id + 1
        self._save()
        return new_id

    def add_entry_with_dedup(
        self,
        source_type: str,
        content: str,
        source_id: str = "",
        title: str = "",
        summary: str = "",
        project_id: str | None = None,
    ) -> tuple[int, str]:
        clean_content = (content or "").strip()
        if not clean_content:
            return 0, "skipped"
        data = self._load()
        entries: list[dict[str, Any]] = data.get("entries", [])
        now = _now_iso()
        sid = (source_id or "").strip()
        content_hash = _compute_content_hash(clean_content)
        pid = (project_id or DEFAULT_PROJECT_ID).upper()
        for e in entries:
            if e.get("content_hash") and (e.get("project_id") or DEFAULT_PROJECT_ID).upper() == pid:
                if e.get("content_hash") == content_hash:
                    return int(e.get("id")), "skipped"
        if sid:
            for e in entries:
                if (
                    e.get("source_type") == source_type
                    and (e.get("source_id") or "") == sid
                    and (e.get("project_id") or DEFAULT_PROJECT_ID).upper() == pid
                ):
                    e.update(
                        {
                            "created_at": now,
                            "title": title or "",
                            "content": clean_content,
                            "summary": summary or "",
                            "content_hash": content_hash,
                            "agent_summary": "",
                            "agent_summary_status": "pending",
                            "project_id": pid,
                        }
                    )
                    self._save()
                    return int(e.get("id")), "updated"
        new_id = int(data.get("next_id", 1))
        entry = {
            "id": new_id,
            "created_at": now,
            "source_type": source_type,
            "source_id": sid,
            "title": title or "",
            "content": clean_content,
            "summary": summary or "",
            "content_hash": content_hash,
            "agent_summary": "",
            "agent_summary_status": "pending",
            "project_id": pid,
        }
        entries.append(entry)
        data["next_id"] = new_id + 1
        self._save()
        return new_id, "added"

    def _sorted_entries(self) -> list[dict[str, Any]]:
        data = self._load()
        entries: list[dict[str, Any]] = data.get("entries", [])
        return sorted(entries, key=lambda e: e.get("created_at", ""), reverse=True)

    def search(self, keyword: str, limit: int = 50, project_id: str | None = None) -> list[dict[str, Any]]:
        if not keyword or not keyword.strip():
            return self.list_recent(limit=limit, project_id=project_id)
        kw = keyword.strip()
        terms = [t.strip() for t in kw.split() if t.strip()]
        if not terms:
            return self.list_recent(limit=limit, project_id=project_id)
        pid = (project_id or DEFAULT_PROJECT_ID).upper()
        entries = [
            self._row_to_dict(e)
            for e in self._sorted_entries()
            if (e.get("project_id") or DEFAULT_PROJECT_ID).upper() == pid
        ]
        results: list[dict[str, Any]] = []
        for e in entries:
            text = f"{e.get('title','')} {e.get('summary','')} {e.get('content','')}"
            if all(t in text for t in terms):
                results.append(e)
        if not results:
            return []

        def score(e: dict[str, Any]) -> int:
            text = f"{e.get('title','')} {e.get('summary','')} {e.get('content','')}"
            return sum(1 for t in terms if t in text)

        results.sort(key=lambda e: (score(e), e.get("created_at", "")), reverse=True)
        return results[:limit]

    def delete_entry(self, entry_id: int) -> bool:
        data = self._load()
        entries: list[dict[str, Any]] = data.get("entries", [])
        before = len(entries)
        entries[:] = [e for e in entries if int(e.get("id")) != int(entry_id)]
        changed = len(entries) < before
        if changed:
            self._save()
        return changed

    def list_recent(self, limit: int = 50, project_id: str | None = None) -> list[dict[str, Any]]:
        pid = (project_id or DEFAULT_PROJECT_ID).upper()
        entries = [
            e
            for e in self._sorted_entries()
            if (e.get("project_id") or DEFAULT_PROJECT_ID).upper() == pid
        ][:limit]
        return [self._row_to_dict(e) for e in entries]

    def update_agent_summary(self, entry_id: int, summary: str, status: str) -> bool:
        data = self._load()
        entries: list[dict[str, Any]] = data.get("entries", [])
        for e in entries:
            if int(e.get("id")) == int(entry_id):
                e["agent_summary"] = summary or ""
                e["agent_summary_status"] = status
                self._save()
                return True
        return False

    def list_import_history(self, limit: int = 20, project_id: str | None = None) -> list[dict[str, Any]]:
        pid = (project_id or DEFAULT_PROJECT_ID).upper()
        entries = [
            e
            for e in self._sorted_entries()
            if (e.get("project_id") or DEFAULT_PROJECT_ID).upper() == pid
        ][:limit]
        return [self._row_to_dict(e) for e in entries]

    def get_recent_for_agent(
        self,
        limit: int = 10,
        max_content_len: int = 3000,
        demand_only: bool = True,
        include_test_cases: bool = False,
        project_id: str | None = None,
    ) -> str:
        pid = (project_id or DEFAULT_PROJECT_ID).upper()
        entries = [
            e
            for e in self._sorted_entries()
            if (e.get("project_id") or DEFAULT_PROJECT_ID).upper() == pid
        ]
        if demand_only and not include_test_cases:
            types = set(DEMAND_SOURCE_TYPES)
        elif include_test_cases:
            types = set((*DEMAND_SOURCE_TYPES, TEST_CASES_SOURCE_TYPE))
        else:
            types = None
        if types is not None:
            entries = [e for e in entries if e.get("source_type") in types]
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for e in entries:
            sid = (e.get("source_id") or "").strip()
            key = f"{e.get('source_type')}:{sid}" if sid else f"_{e.get('id')}"
            if key not in seen:
                seen.add(key)
                unique.append(self._row_to_dict(e))
            if len(unique) >= limit:
                break
        if not unique:
            return ""
        parts: list[str] = []
        per_len = max(200, max_content_len // len(unique))
        for e in unique:
            title = (e.get("title") or "").strip() or f"[{e.get('source_type')}]"
            summary = (e.get("summary") or e.get("content", "")).strip()
            if len(summary) > per_len:
                summary = summary[:per_len] + "..."
            parts.append(f"【{title}】{e.get('created_at', '')}\n{summary}")
        return "\n\n---\n\n".join(parts)

    def get_all_demands_full_for_chat(
        self,
        limit: int = 30,
        max_total_chars: int = 80000,
        include_test_cases: bool = True,
        project_id: str | None = None,
    ) -> str:
        pid = (project_id or DEFAULT_PROJECT_ID).upper()
        entries = [
            e
            for e in self._sorted_entries()
            if (e.get("project_id") or DEFAULT_PROJECT_ID).upper() == pid
        ]
        types = set((*DEMAND_SOURCE_TYPES, TEST_CASES_SOURCE_TYPE)) if include_test_cases else set(DEMAND_SOURCE_TYPES)
        entries = [e for e in entries if e.get("source_type") in types]
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for e in entries:
            sid = (e.get("source_id") or "").strip()
            key = f"{e.get('source_type')}:{sid}" if sid else f"_{e.get('id')}"
            if key not in seen:
                seen.add(key)
                unique.append(self._row_to_dict(e))
            if len(unique) >= limit:
                break
        if not unique:
            return ""
        parts: list[str] = []
        total = 0
        for e in unique:
            title = (e.get("title") or "").strip() or f"[{e.get('source_type')}]"
            content = (e.get("content") or e.get("summary") or "").strip()
            block = f"【{title}】{e.get('created_at', '')}\n\n{content}"
            if total + len(block) > max_total_chars:
                block = block[: max_total_chars - total - 20] + "\n...(已截断)"
            parts.append(block)
            total += len(block)
            if total >= max_total_chars:
                break
        return "\n\n---\n\n".join(parts)

    def get_entry_content(self, source_type: str, source_id: str, project_id: str | None = None) -> str | None:
        data = self._load()
        entries: list[dict[str, Any]] = data.get("entries", [])
        pid = (project_id or DEFAULT_PROJECT_ID).upper()
        for e in entries:
            if (
                e.get("source_type") == source_type
                and (e.get("source_id") or "") == source_id
                and (e.get("project_id") or DEFAULT_PROJECT_ID).upper() == pid
            ):
                return str(e.get("content") or "")
        return None

    def list_for_browse(
        self,
        source_type_filter: str = "",
        limit: int = 50,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        pid = (project_id or DEFAULT_PROJECT_ID).upper()
        entries = [
            e
            for e in self._sorted_entries()
            if (e.get("project_id") or DEFAULT_PROJECT_ID).upper() == pid
        ]
        if source_type_filter and source_type_filter.strip():
            entries = [e for e in entries if e.get("source_type") == source_type_filter.strip()]
        entries = entries[:limit]
        return [self._row_to_dict(e) for e in entries]


if _SQLITE_AVAILABLE:
    _backend: _MemoryBackend = SqliteBackend()
else:
    _backend = JsonFileBackend()


def add_entry(
    source_type: str,
    content: str,
    source_id: str = "",
    title: str = "",
    summary: str = "",
    project_id: str | None = None,
) -> int:
    return _backend.add_entry(
        source_type,
        content,
        source_id=source_id,
        title=title,
        summary=summary,
        project_id=project_id,
    )


def add_entry_with_dedup(
    source_type: str,
    content: str,
    source_id: str = "",
    title: str = "",
    summary: str = "",
    project_id: str | None = None,
) -> tuple[int, str]:
    return _backend.add_entry_with_dedup(
        source_type,
        content,
        source_id=source_id,
        title=title,
        summary=summary,
        project_id=project_id,
    )


def search(keyword: str, limit: int = 50, project_id: str | None = None) -> list[dict[str, Any]]:
    return _backend.search(keyword, limit=limit, project_id=project_id)


def delete_entry(entry_id: int) -> bool:
    return _backend.delete_entry(entry_id)


def list_recent(limit: int = 50, project_id: str | None = None) -> list[dict[str, Any]]:
    return _backend.list_recent(limit=limit, project_id=project_id)


def update_agent_summary(entry_id: int, summary: str, status: str) -> bool:
    return _backend.update_agent_summary(entry_id, summary, status)


def list_import_history(limit: int = 20, project_id: str | None = None) -> list[dict[str, Any]]:
    return _backend.list_import_history(limit=limit, project_id=project_id)


def get_recent_for_agent(
    limit: int = 10,
    max_content_len: int = 3000,
    demand_only: bool = True,
    include_test_cases: bool = False,
    project_id: str | None = None,
) -> str:
    return _backend.get_recent_for_agent(
        limit=limit,
        max_content_len=max_content_len,
        demand_only=demand_only,
        include_test_cases=include_test_cases,
        project_id=project_id,
    )


def get_all_demands_full_for_chat(
    limit: int = 30,
    max_total_chars: int = 80000,
    include_test_cases: bool = True,
    project_id: str | None = None,
) -> str:
    return _backend.get_all_demands_full_for_chat(
        limit=limit,
        max_total_chars=max_total_chars,
        include_test_cases=include_test_cases,
        project_id=project_id,
    )


def get_entry_content(
    source_type: str,
    source_id: str,
    project_id: str | None = None,
) -> str | None:
    return _backend.get_entry_content(source_type, source_id, project_id=project_id)


def list_for_browse(
    source_type_filter: str = "",
    limit: int = 50,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    return _backend.list_for_browse(
        source_type_filter=source_type_filter,
        limit=limit,
        project_id=project_id,
    )
