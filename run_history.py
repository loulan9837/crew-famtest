# -*- coding: utf-8 -*-
"""
生成用例历史记录存储：列表的增删查、持久化到 output/generate_history.json。
供 app_ui 生成用例入口使用，符合项目规范（session_state 隔离、路径校验）。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
HISTORY_JSON = os.path.join(OUTPUT_DIR, "generate_history.json")
_MAX_RECORDS = 100

DEFAULT_PROJECT_ID = "FAMBASE"


def _ensure_output_in_allowed_dir(path: str) -> bool:
    """校验路径在 output 目录内，防御目录遍历。"""
    try:
        abs_path = os.path.abspath(path)
        abs_output = os.path.abspath(OUTPUT_DIR)
        return abs_path.startswith(abs_output)
    except Exception:
        return False


def _load_history() -> list[dict[str, Any]]:
    """从 JSON 加载历史记录列表，按时间倒序。"""
    if not os.path.isfile(HISTORY_JSON):
        return []
    try:
        with open(HISTORY_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("items") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        if not isinstance(items, list):
            return []
        # 兼容旧版本：未写入 project_id 的记录视为 FAMBASE
        for r in items:
            if "project_id" not in r or not r.get("project_id"):
                r["project_id"] = DEFAULT_PROJECT_ID
        items.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return items
    except Exception:
        return []


def _save_history(items: list[dict[str, Any]]) -> bool:
    """将历史记录列表写入 JSON。"""
    if not _ensure_output_in_allowed_dir(HISTORY_JSON):
        return False
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(HISTORY_JSON, "w", encoding="utf-8") as f:
            json.dump({"items": items}, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _slug_for_filename(s: str, max_len: int = 30) -> str:
    """将标题转为安全文件名片段，去除非法字符。"""
    s = re.sub(r'[<>:"/\\|?*]', "", (s or "").strip())
    s = s[:max_len] if s else "需求"
    return s or "需求"


# 对外暴露供 app_ui 使用
slug_for_filename = _slug_for_filename


def add_run_record(
    source_type: str,
    demand_title: str,
    result_str: str,
    excel_path: str | None = None,
    txt_path: str | None = None,
    sheets_url: str | None = None,
    project_id: str | None = None,
) -> str | None:
    """追加一条生成记录。返回 record_id，失败返回 None。
    result_str 可存全文或预览；若提供 txt_path 则展示时优先从文件读取。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    rid = datetime.now().strftime("%Y%m%d_%H%M%S")
    pid = (project_id or DEFAULT_PROJECT_ID).upper()
    preview = str(result_str or "")[:800]
    record = {
        "id": rid,
        "source_type": source_type,
        "demand_title": (demand_title or "")[:80],
        "timestamp": ts,
        "result_str": preview,
        "excel_path": excel_path or "",
        "txt_path": txt_path or "",
        "sheets_url": sheets_url or "",
        "project_id": pid,
    }
    items = _load_history()
    items.insert(0, record)
    if len(items) > _MAX_RECORDS:
        items = items[:_MAX_RECORDS]
    return rid if _save_history(items) else None


def list_run_records(
    keyword: str = "",
    limit: int = 20,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """按关键字过滤并返回历史记录，按时间倒序。limit 默认 20。"""
    items = _load_history()
    pid = (project_id or DEFAULT_PROJECT_ID).upper()
    items = [r for r in items if (r.get("project_id") or DEFAULT_PROJECT_ID).upper() == pid]
    if keyword:
        kw = keyword.strip().lower()
        items = [r for r in items if kw in (r.get("demand_title") or "").lower() or kw in (r.get("source_type") or "").lower()]
    return items[:limit]


def delete_run_record(record_id: str) -> bool:
    """根据 id 删除一条记录。"""
    items = _load_history()
    items = [r for r in items if r.get("id") != record_id]
    return _save_history(items)


def get_full_result(record: dict[str, Any], extra_allowed_dirs: list[str] | None = None) -> str:
    """获取记录的完整结果文本：优先从 txt_path 读取，否则用 result_str。
    extra_allowed_dirs: 额外允许读取的目录（如自定义 workspace_path），用于路径校验。"""
    txt_path = record.get("txt_path") or ""
    if not txt_path or not os.path.isfile(txt_path):
        return str(record.get("result_str") or "")
    _allowed = _ensure_output_in_allowed_dir(txt_path)
    if not _allowed and extra_allowed_dirs:
        try:
            abs_p = os.path.abspath(txt_path)
            for d in extra_allowed_dirs:
                if d and os.path.abspath(d) and abs_p.startswith(os.path.abspath(d)):
                    _allowed = True
                    break
        except Exception:
            pass
    if _allowed:
        try:
            with open(txt_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass
    return str(record.get("result_str") or "")


def get_excel_filename(record: dict[str, Any]) -> str:
    """生成下载用 Excel 文件名：测试用例_{标题简写}_{timestamp}.xlsx"""
    title = _slug_for_filename(record.get("demand_title", ""), 20)
    ts = (record.get("timestamp") or "").replace(" ", "_").replace(":", "")
    return f"测试用例_{title}_{ts}.xlsx"
