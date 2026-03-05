# -*- coding: utf-8 -*-
"""
Agent 知识库构建服务：从 memory_store + project_memory 汇总，调用 LLM 生成结构化知识库。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
AGENT_KNOWLEDGE_PATH = os.path.join(CONFIG_DIR, "agent_knowledge.md")
AGENT_KNOWLEDGE_META_PATH = os.path.join(CONFIG_DIR, "agent_knowledge_meta.json")
PROJECT_MEMORY_PATH = os.path.join(CONFIG_DIR, "project_memory.md")
MAX_INPUT_CHARS = 45000  # 控制 LLM 输入长度
KNOWLEDGE_DAYS_STALE = 7

DEFAULT_PROJECT_ID = "FAMBASE"


def _normalize_project_id(project_id: str | None) -> str:
    pid = (project_id or DEFAULT_PROJECT_ID).upper()
    if pid not in ("FAMBASE", "RM11"):
        return DEFAULT_PROJECT_ID
    return pid


def _get_project_memory_path(project_id: str | None = None) -> str:
    pid = _normalize_project_id(project_id)
    if pid == "RM11":
        return os.path.join(CONFIG_DIR, "project_memory_rm11.md")
    return PROJECT_MEMORY_PATH


def _get_knowledge_paths(project_id: str | None = None) -> tuple[str, str]:
    pid = _normalize_project_id(project_id)
    if pid == "RM11":
        return (
            os.path.join(CONFIG_DIR, "agent_knowledge_rm11.md"),
            os.path.join(CONFIG_DIR, "agent_knowledge_rm11_meta.json"),
        )
    return AGENT_KNOWLEDGE_PATH, AGENT_KNOWLEDGE_META_PATH


def _get_raw_content_for_knowledge(project_id: str | None = None) -> str:
    """从 memory_entries 收集需求、全回归、run_summary，按时间排序，截断至 max_chars。"""
    try:
        from memory_store import list_for_browse
    except ImportError:
        return ""

    entries = list_for_browse(source_type_filter="", limit=80, project_id=_normalize_project_id(project_id))
    if not entries:
        return ""

    parts: list[str] = []
    total = 0
    for e in entries:
        title = (e.get("title") or "").strip() or f"[{e.get('source_type')}]"
        content = (e.get("content") or e.get("summary") or "").strip()
        block = f"【{title}】{e.get('created_at', '')}\n来源: {e.get('source_type', '')}\n\n{content}"
        if total + len(block) > MAX_INPUT_CHARS:
            remaining = MAX_INPUT_CHARS - total - 100
            if remaining > 0:
                block = block[:remaining] + "\n...(已截断)"
                parts.append(block)
                total += len(block)
            break
        parts.append(block)
        total += len(block)
    return "\n\n---\n\n".join(parts)


def _load_project_memory(project_id: str | None = None) -> str:
    """读取当前项目的 project_memory 文件内容。"""
    path = _get_project_memory_path(project_id)
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _get_last_updated(project_id: str | None = None) -> str | None:
    """返回知识库最后更新时间，格式 YYYY-MM-DD HH:MM。不存在则 None。"""
    knowledge_path, meta_path = _get_knowledge_paths(project_id)
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("last_updated")
        except Exception:
            pass
    if os.path.isfile(knowledge_path):
        try:
            mtime = os.path.getmtime(knowledge_path)
            return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    return None


def get_last_updated(project_id: str | None = None) -> str | None:
    """返回知识库最后更新时间，格式 YYYY-MM-DD HH:MM。不存在则 None。供 UI 展示。"""
    return _get_last_updated(project_id)


def is_knowledge_stale(project_id: str | None = None) -> bool:
    """知识库是否已过期（≥7 天未更新）。"""
    last = _get_last_updated(project_id)
    if not last:
        return True
    try:
        dt = datetime.strptime(last, "%Y-%m-%d %H:%M")
        delta = datetime.now() - dt
        return delta.days >= KNOWLEDGE_DAYS_STALE
    except Exception:
        return True


def build_agent_knowledge(
    gemini_key: str = "",
    gemini_model: str = "",
) -> tuple[bool, str]:
    """
    构建 Agent 知识库，覆盖写入 agent_knowledge.md。
    返回 (是否成功, 错误信息或空字符串)。
    """
    key = (gemini_key or "").strip() or os.environ.get("GEMINI_API_KEY")
    if not key:
        return False, "请先配置 GEMINI_API_KEY"

    # 当前项目：若调用方未显式传入，可通过环境变量 APP_CURRENT_PROJECT 约定
    project_id = _normalize_project_id(os.environ.get("APP_CURRENT_PROJECT"))

    raw_store = _get_raw_content_for_knowledge(project_id)
    raw_project = _load_project_memory(project_id)
    combined = (raw_project + "\n\n---\n\n【记忆库内容】\n\n" + raw_store).strip()
    if not combined or (not raw_store and not raw_project):
        # 空数据：写入最小模板
        minimal = """# Agent 知识库

## 一、产品域概览
暂无导入数据。请先在「项目记忆」页导入需求文档与全回归用例后，点击「刷新知识库」。
"""
        try:
            knowledge_path, meta_path = _get_knowledge_paths(project_id)
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(knowledge_path, "w", encoding="utf-8") as f:
                f.write(minimal)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump({"last_updated": now_str}, f, ensure_ascii=False, indent=2)
            return True, ""
        except Exception as e:
            return False, str(e)

    # 限制输入长度
    if len(combined) > MAX_INPUT_CHARS:
        combined = combined[:MAX_INPUT_CHARS] + "\n...(已截断)"

    safe_content = combined
    try:
        from crew_test import desensitize_for_llm
        safe_content = desensitize_for_llm(combined)[:MAX_INPUT_CHARS]
    except ImportError:
        pass

    prompt = """你是一位资深需求与测试架构师。请基于以下【项目记忆 + 记忆库内容】，产出一份结构化的《Agent 知识库》Markdown 文档，供 4 个 Agent 在生成测试用例时作为长期推理背景使用。

要求：
1. 按以下结构组织，章节可精简但必须覆盖：
   - 一、产品域概览（项目简述、关键域：群组聊天、群内Live、Coins、礼物打赏、V3后台等）
   - 二、核心模块与职责（群组聊天、群内Live、Coins与礼物打赏等的规则摘要与常见风险）
   - 三、需求文档变更脉络（按时间线：日期+文档名+变更要点）
   - 四、全回归用例覆盖地图（已覆盖场景、薄弱区域）
   - 五、历史运行经验（可选：常见缺陷模式、已修正问题）
2. 做总结式消化，不要大段原文粘贴。
3. 直接输出完整 Markdown，不要多余说明。

【项目记忆 + 记忆库内容】
""" + safe_content

    try:
        from crew_test import _resolve_gemini_model
        model = _resolve_gemini_model(
            (gemini_model or "").strip() or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
        )
    except ImportError:
        model = (gemini_model or "").strip() or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
    for attempt in range(3):
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI

            llm = ChatGoogleGenerativeAI(model=model, google_api_key=key, temperature=0.3)
            msg = llm.invoke(prompt)
            result = (msg.content or "").strip()
            if not result:
                raise ValueError("LLM 返回为空")

            knowledge_path, meta_path = _get_knowledge_paths(project_id)
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(knowledge_path, "w", encoding="utf-8") as f:
                f.write(result)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump({"last_updated": now_str}, f, ensure_ascii=False, indent=2)
            return True, ""
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "503" in err_str or "timeout" in err_str.lower():
                if attempt < 2:
                    import time
                    time.sleep(5)
                    continue
            return False, err_str
    return False, "构建失败，请稍后重试"


def load_agent_knowledge(project_id: str | None = None) -> str:
    """读取当前项目的 agent_knowledge 内容，不存在或为空则返回空字符串。"""
    knowledge_path, _ = _get_knowledge_paths(project_id)
    if not os.path.isfile(knowledge_path):
        return ""
    try:
        with open(knowledge_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""
