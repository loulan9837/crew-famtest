# -*- coding: utf-8 -*-
"""
可视化界面：上传/粘贴需求 → 四 Agent 流水线 → 测试用例 Excel
文案可在 config/ui_texts.yaml 中编辑，无需改代码。
"""
from __future__ import annotations

import json
import os
import sys

import streamlit as st

# 将项目根目录加入 path，以便导入 crew_test
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

UI_TEXTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "ui_texts.yaml")

from crew_test import (
    AGENTS_CONFIG_PATH,
    PROJECT_MEMORY_PATH,
    _export_to_excel,
    _parse_markdown_tables,
    _resolve_gemini_model,
    chat_with_document_agent,
    export_tables_to_excel_bytes,
    generate_incremental_cases,
    get_project_context_for_agent,
    load_agents_config,
    load_project_memory,
    parse_test_cases_file,
    parse_uploaded_files,
    update_project_memory,
)

try:
    from memory_store import (
        TEST_CASES_SOURCE_TYPE,
        add_entry,
        add_entry_with_dedup,
        delete_entry,
        get_entry_content,
        list_import_history,
        list_recent,
        search,
        update_agent_summary,
        DESIGN_MOCKUP_SOURCE_TYPE,
    )
    MEMORY_AVAILABLE = True
except Exception as e:
    # 线上（如 Streamlit Cloud）若未启用 sqlite3，或 memory_store 后端初始化失败，
    # 则将项目记忆相关功能降级为不可用状态，避免拖垮整个应用。
    MEMORY_AVAILABLE = False
from pipeline_service import run_upload_to_cases
from risk_report_service import generate_risk_assessment_report

CONFIG_DIR = os.path.dirname(AGENTS_CONFIG_PATH)
DEFAULTS_PATH = os.path.join(CONFIG_DIR, "defaults.json")
MODELS_CONFIG_PATH = os.path.join(CONFIG_DIR, "models.yaml")
WORKBENCH_APPS_PATH = os.path.join(CONFIG_DIR, "workbench_apps.yaml")
VERSION_PATH = os.path.join(CONFIG_DIR, "version.yaml")
LOCAL_WORKSPACE_PATH = os.path.join(CONFIG_DIR, "local_workspace.yaml")
OUTPUT_DIR = "output"
LAST_RUN_JSON = os.path.join(OUTPUT_DIR, "last_run.json")


def _get_output_dir() -> str:
    """获取输出目录：优先使用 config/local_workspace.yaml 中的 workspace_path，否则用 output/。"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    default_dir = os.path.join(base_dir, OUTPUT_DIR)

    candidate = None
    if os.path.isfile(LOCAL_WORKSPACE_PATH):
        try:
            import yaml
            with open(LOCAL_WORKSPACE_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            wp = (data.get("workspace_path") or "").strip()
            if wp:
                if os.path.isabs(wp):
                    candidate = os.path.abspath(wp)
                else:
                    candidate = os.path.abspath(os.path.join(base_dir, wp))
        except Exception:
            candidate = None

    # 防御目录遍历：仅允许项目根目录内的子目录作为工作区
    if candidate:
        try:
            if os.path.commonpath([base_dir, candidate]) == base_dir:
                os.makedirs(candidate, exist_ok=True)
                return candidate
        except Exception:
            # 若目录不可用或创建失败，回退到默认 output 目录
            pass

    try:
        os.makedirs(default_dir, exist_ok=True)
        return default_dir
    except Exception:
        # 最保守的兜底：回退到相对路径 output/，避免因权限问题导致流程完全中断
        return OUTPUT_DIR

MODULE_RUN = "run"
MODULE_AGENTS = "agents"
MODULE_MEMORY = "memory"
MODULE_CHAT = "chat"
MODULE_RISK_REPORT = "risk_report"
MODULE_SETTINGS = "settings"

PROJECT_FAMBASE = "FAMBASE"
PROJECT_RM11 = "RM11"


def _normalize_project_id(project_id: str | None) -> str:
    pid = (project_id or PROJECT_FAMBASE).upper()
    if pid not in (PROJECT_FAMBASE, PROJECT_RM11):
        return PROJECT_FAMBASE
    return pid


def _get_current_project() -> str:
    """获取当前项目 ID，并同步到环境变量，供后端逻辑使用。"""
    pid = _normalize_project_id(st.session_state.get("current_project"))
    st.session_state["current_project"] = pid
    os.environ["APP_CURRENT_PROJECT"] = pid
    return pid


def _get_project_display_name(project_id: str) -> str:
    pid = _normalize_project_id(project_id)
    if pid == PROJECT_FAMBASE:
        return "Fambase"
    if pid == PROJECT_RM11:
        return "RM11"
    return pid


def _has_unsaved_project_state() -> bool:
    """粗粒度检测当前项目下是否存在未保存的用户输入，用于项目切换前确认。"""
    s = st.session_state
    if (s.get("run_paste_content") or "").strip():
        return True
    if s.get("run_upload_files_cache"):
        return True
    if (s.get("mem_demand_paste") or "").strip():
        return True
    if s.get("memory_test_cases_upload_cache"):
        return True
    if (s.get("test_cases_paste") or "").strip():
        return True
    if (s.get("chat_paste_doc") or "").strip():
        return True
    if (s.get("risk_report_paste") or "").strip():
        return True
    return False


def _clear_project_related_state() -> None:
    """清理与项目强相关的临时 UI 状态，避免多项目串数据。"""
    prefixes = [
        "run_",
        "mem_",
        "chat_",
        "risk_report_",
        "app_run_",
        "app_memory_",
        "app_doc_chat_",
        "app_risk_report_",
    ]
    for key in list(st.session_state.keys()):
        if any(key.startswith(p) for p in prefixes):
            st.session_state.pop(key, None)

class _MemoryUpload:
    """内存中的上传文件封装，兼容 parse_uploaded_files / parse_test_cases_file 接口。"""

    def __init__(self, name: str, data: bytes) -> None:
        self.name = name
        self._data = data or b""

    def read(self) -> bytes:
        return self._data

    def getvalue(self) -> bytes:
        return self._data


SUMMARY_PROMPT = (
    "你是一位资深需求与测试架构师。这是一份新上传或导入的测试或需求文档。"
    "请用 150 字以内的中文，总结该文档主要涵盖的业务模块、核心操作流程以及增删改的重点逻辑。"
    "不要输出多余解释，直接给出总结。"
)


def _generate_entry_summary(entry_id: int, content: str, gemini_key: str = "") -> tuple[bool, str]:
    """调用 Gemini 生成条目摘要。返回 (是否成功, 失败时的错误信息)。"""
    if not (content or "").strip():
        update_agent_summary(entry_id, "", "failed")
        return False, "内容为空"
    text = (content or "").strip()[:8000]
    key = (gemini_key or "").strip() or os.environ.get("GEMINI_API_KEY", "")
    if not key:
        update_agent_summary(entry_id, "", "failed")
        return False, "未配置 Gemini API Key"
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
        raw_model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
        model = _resolve_gemini_model(raw_model)
        llm = ChatGoogleGenerativeAI(model=model, google_api_key=key, temperature=0.2)
        msg = llm.invoke(f"{SUMMARY_PROMPT}\n\n---\n\n{text}")
        summary = (msg.content or "").strip()
        if summary:
            update_agent_summary(entry_id, summary, "success")
            return True, ""
    except Exception as ex:
        update_agent_summary(entry_id, "", "failed")
        return False, str(ex)
    update_agent_summary(entry_id, "", "failed")
    return False, "生成为空"


def _parse_design_image_with_gemini(
    image_bytes: bytes,
    mime_type: str,
    gemini_key: str = "",
) -> tuple[str, str]:
    """
    调用 Gemini Vision 解析设计图，返回结构化文本描述。
    返回 (description_text, error_message)。error_message 为空字符串表示成功。
    """
    key = (gemini_key or "").strip() or os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return "", "未配置 Gemini API Key"
    if not image_bytes:
        return "", "图片内容为空"

    design_vision_prompt = (
        "你是一位资深 UI/UX 测试工程师。请分析这张界面设计图，输出结构化描述，"
        "供自动化测试用例生成系统参考。\n\n"
        "输出格式：\n"
        "## 页面 / 模块名称\n（根据图中标题或推断得出）\n\n"
        "## 页面层级与布局\n（顶栏、底栏、侧栏、主内容区大体布局）\n\n"
        "## 关键 UI 组件\n（按钮、输入框、列表项、弹窗等，说明标签文案和状态）\n\n"
        "## 可见的交互入口\n（Tab 切换、下拉菜单、长按操作等）\n\n"
        "## 状态与条件展示\n（空态、加载态、错误态、权限不足态等，如图中有体现）\n\n"
        "## 关键文案\n（图中重要的 UI 文案、错误提示、占位符文字等）\n\n"
        "只输出上述结构，不加多余说明。"
    )

    try:
        import base64
        from langchain_core.messages import HumanMessage
        from langchain_google_genai import ChatGoogleGenerativeAI

        raw_model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
        model = _resolve_gemini_model(raw_model)
        llm = ChatGoogleGenerativeAI(model=model, google_api_key=key, temperature=0.1)

        b64 = base64.b64encode(image_bytes).decode("utf-8")
        message = HumanMessage(
            content=[
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
                {"type": "text", "text": design_vision_prompt},
            ]
        )
        resp = llm.invoke([message])
        text = (getattr(resp, "content", None) or str(resp) or "").strip()
        if not text:
            return "", "Gemini 返回内容为空，请确认图片清晰度"
        return text, ""
    except Exception as ex:  # pragma: no cover - 依赖外部服务
        err = str(ex)
        return "", f"解析失败：{err}"


def _save_last_run(r: dict) -> None:
    """持久化上次运行结果到 JSON，刷新后仍可展示。"""
    try:
        import json
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        out = {
            "excel_path": r.get("excel_path"),
            "txt_path": r.get("txt_path"),
            "sheets_url": r.get("sheets_url"),
            "demand_title": r.get("demand_title", ""),
            "timestamp": r.get("timestamp", ""),
        }
        with open(LAST_RUN_JSON, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _load_last_run() -> dict | None:
    """从 JSON 恢复上次运行结果（刷新后使用）。"""
    try:
        import json
        if not os.path.isfile(LAST_RUN_JSON):
            return None
        with open(LAST_RUN_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        excel_path = data.get("excel_path") or ""
        txt_path = data.get("txt_path") or ""
        if not excel_path and not txt_path:
            return None
        result_str = ""
        if txt_path and os.path.isfile(txt_path):
            with open(txt_path, "r", encoding="utf-8") as f:
                result_str = f.read()
        return {
            "excel_path": excel_path if os.path.isfile(excel_path) else None,
            "txt_path": txt_path,
            "sheets_url": data.get("sheets_url"),
            "result_str": result_str,
            "step_outputs": [],
            "timestamp": data.get("timestamp", ""),
            "demand_title": data.get("demand_title", ""),
        }
    except Exception:
        return None


def _load_models() -> tuple[list[tuple[str, str]], str]:
    """从 config/models.yaml 读取模型列表，返回 ((key, label), ...) 与默认 key。"""
    try:
        import yaml
        if os.path.isfile(MODELS_CONFIG_PATH):
            with open(MODELS_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                models = data.get("models") or []
                out = []
                default_key = "gemini-2.5-flash-lite"
                for m in models:
                    k = (m.get("key") or "").strip()
                    label = (m.get("label") or k).strip()
                    if k:
                        out.append((k, label))
                        if m.get("default") is True:
                            default_key = k
                if out:
                    return out, default_key
    except Exception:
        pass
    # 回退兜底：本地未能读取 config/models.yaml 时的默认模型列表（带出常用额度上限）
    return [
        ("gemini-2.5-flash-lite", "2.5 Flash-Lite（4K RPM / 4M TPM / 日请求无限制 · 免费额度高，推荐）"),
        ("gemini-2.5-flash", "2.5 Flash（1K RPM / 1M TPM / 日 10K 次 · 质量与速度平衡）"),
        ("gemini-2.5-pro", "2.5 Pro（高质量 · 具体配额以控制台为准）"),
    ], "gemini-2.5-flash-lite"


def _load_version() -> dict:
    """从 config/version.yaml 读取版本号，用于验证线上代码已成功更新。"""
    try:
        import yaml
        if os.path.isfile(VERSION_PATH):
            with open(VERSION_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                v = data.get("version", "")
                b = data.get("build_time", "")
                return {"version": str(v) if v else "", "build_time": str(b) if b else ""}
    except Exception:
        pass
    return {"version": "", "build_time": ""}


def _load_workbench_apps(T: dict) -> list[dict]:
    """从 config/workbench_apps.yaml 读取工作台模块列表，仅返回 enabled 且按 order 排序的项。"""
    try:
        import yaml
        if os.path.isfile(WORKBENCH_APPS_PATH):
            with open(WORKBENCH_APPS_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                apps = data.get("apps") or []
                out = [a for a in apps if a.get("enabled", True)]
                out.sort(key=lambda x: (x.get("order", 99), x.get("id", "")))
                for a in out:
                    label_key = a.get("label_key")
                    a["label"] = _get_text(T, label_key) or a.get("label", a.get("id", ""))
                return out
    except Exception:
        pass
    return [
        {"id": MODULE_RUN, "label": _get_text(T, "tabs.run") or "生成用例"},
        {"id": MODULE_AGENTS, "label": _get_text(T, "tabs.agents") or "编辑 Agent"},
        {"id": MODULE_MEMORY, "label": _get_text(T, "tabs.memory") or "项目记忆"},
        {"id": MODULE_CHAT, "label": _get_text(T, "tabs.chat") or "文档问答"},
        {"id": MODULE_SETTINGS, "label": _get_text(T, "app.settings") or "设置"},
    ]


def _get_module_state_key(module_id: str, suffix: str) -> str:
    return f"app_{module_id}_{suffix}"


def _parse_cases_md_to_rows(cases_md: str) -> list[list[str]]:
    """将 Markdown 用例表解析为行列表（含表头），无表或解析失败返回空列表。"""
    if not (cases_md or "").strip():
        return []
    tables = _parse_markdown_tables(cases_md)
    if not tables:
        return []
    return tables[0]


def _rows_to_df_like(rows: list[list[str]]) -> list[dict[str, str]]:
    """将表格行转换为 DataFrame-like 结构（list[dict]），首行为表头。"""
    if not rows or len(rows) < 2:
        return []
    header = [str(c or "").strip() for c in rows[0]]
    data_rows = rows[1:]
    df_like: list[dict[str, str]] = []
    for r in data_rows:
        row_dict: dict[str, str] = {}
        for idx, col in enumerate(header):
            val = str(r[idx] if idx < len(r) else "").strip()
            row_dict[col] = val
        df_like.append(row_dict)
    return df_like


def _update_cases_state_from_md(prd_text: str, cases_md: str) -> None:
    """根据主流程生成的用例 Markdown 更新 session_state 中的当前/基线用例状态。"""
    rows = _parse_cases_md_to_rows(cases_md)
    if not rows:
        return
    df_like = _rows_to_df_like(rows)
    if not df_like:
        return
    # 当前与基线表
    st.session_state["current_cases_md"] = cases_md
    st.session_state["base_cases_md"] = cases_md
    st.session_state["current_cases_df"] = df_like
    st.session_state["base_cases_df"] = list(df_like)
    # 增量相关状态重置
    st.session_state["delta_cases_md"] = ""
    st.session_state["delta_cases_df"] = None
    st.session_state["incremental_last_instruction"] = ""
    st.session_state["incremental_last_error"] = ""
    st.session_state["current_prd_text"] = (prd_text or "").strip()
    # 重新计算当前 Excel bytes（用于后续导出全量用例）
    tables = _parse_markdown_tables(cases_md) or []
    if tables:
        excel_bytes = export_tables_to_excel_bytes(tables)
        if excel_bytes:
            st.session_state["current_cases_excel_bytes"] = excel_bytes


def _render_incremental_section(T: dict, defaults: dict) -> None:
    """在主流程结果页下方渲染『用例补充（增量生成）』区块。"""
    cases_md = (st.session_state.get("current_cases_md") or "").strip()
    prd_text = (st.session_state.get("current_prd_text") or "").strip()
    if not cases_md or not prd_text:
        return

    st.divider()
    st.subheader("用例补充（增量生成）")
    st.caption(
        "在已有用例基础上，根据小范围需求变更补充新增场景。只输出增量用例，不重写整表。"
    )

    last_instr = st.session_state.get("incremental_last_instruction", "")
    instruction = st.text_area(
        "补充说明/变更描述",
        value=last_instr,
        height=120,
        key="incremental_instruction",
    ).strip()

    last_err = st.session_state.get("incremental_last_error", "")
    if last_err:
        st.error(last_err)

    col_gen, _ = st.columns([1, 2])
    with col_gen:
        if st.button("生成补充用例", key="incremental_generate"):
            st.session_state["incremental_last_instruction"] = instruction
            out = generate_incremental_cases(
                prd_text=prd_text,
                base_cases_md=cases_md,
                instruction=instruction,
                gemini_key=defaults.get("gemini_key", ""),
                gemini_model=defaults.get("gemini_model", ""),
            )
            if not out.get("ok"):
                st.session_state["incremental_last_error"] = str(out.get("error") or "增量生成失败")
            else:
                delta_md = str(out.get("delta_md") or "").strip()
                delta_rows = _parse_cases_md_to_rows(delta_md)
                delta_df = _rows_to_df_like(delta_rows)
                if not delta_df:
                    st.session_state["incremental_last_error"] = "增量用例解析失败"
                else:
                    st.session_state["delta_cases_md"] = delta_md
                    st.session_state["delta_cases_df"] = delta_df
                    st.session_state["incremental_last_error"] = ""

    delta_df = st.session_state.get("delta_cases_df")
    delta_md = (st.session_state.get("delta_cases_md") or "").strip()
    if not delta_df or not delta_md:
        return

    st.markdown("---")
    st.markdown("**增量用例表（仅新增场景）**")
    st.dataframe(delta_df, use_container_width=True)

    # 导出增量用例 Excel
    delta_tables = _parse_markdown_tables(delta_md) or []
    delta_excel_bytes = export_tables_to_excel_bytes(delta_tables) if delta_tables else None
    if delta_excel_bytes:
        st.download_button(
            "📥 导出补充用例为 Excel",
            data=delta_excel_bytes,
            file_name="增量补充用例.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="dl_incremental_delta",
        )

    col_merge, col_discard = st.columns(2)
    with col_merge:
        if st.button("合并至总用例库", key="incremental_merge"):
            base_rows = _parse_cases_md_to_rows(cases_md)
            d_rows = _parse_cases_md_to_rows(delta_md)
            if not base_rows or not d_rows:
                st.error("合并失败：基线或增量用例表无法解析。")
            else:
                base_header = [str(c or "").strip() for c in base_rows[0]]
                delta_header = [str(c or "").strip() for c in d_rows[0]]
                if base_header != delta_header:
                    st.error("合并失败：增量用例表头与基线不一致，已阻止合并。")
                else:
                    merged_rows = [base_rows[0]] + base_rows[1:] + d_rows[1:]
                    # 通过 crew_test 的导出逻辑重新生成 Markdown
                    header_line = "| " + " | ".join(base_header) + " |"
                    sep_line = "| " + " | ".join("---" for _ in base_header) + " |"
                    body_lines = []
                    for r in merged_rows[1:]:
                        line = "| " + " | ".join(str(c or "").strip() for c in r) + " |"
                        body_lines.append(line)
                    merged_md = "\n".join([header_line, sep_line] + body_lines)
                    st.session_state["current_cases_md"] = merged_md
                    _update_cases_state_from_md(prd_text, merged_md)
                    # 清空增量状态
                    st.session_state["delta_cases_md"] = ""
                    st.session_state["delta_cases_df"] = None
                    st.success("已合并至总用例库。后续导出将包含本次补充用例。")
                    st.experimental_rerun()

    with col_discard:
        if st.button("丢弃本次增量", key="incremental_discard"):
            st.session_state["delta_cases_md"] = ""
            st.session_state["delta_cases_df"] = None
            st.session_state["incremental_last_error"] = ""
            st.success("已丢弃本次增量。")



def _build_agents_snapshot(agents: list, tasks: list) -> dict:
    """从 agents/tasks 构造稳定快照，用于脏标记与保存一致性校验。"""
    snap_agents = [
        {
            "id": (a.get("id") or "").strip(),
            "role": (a.get("role") or "").strip(),
            "goal": (a.get("goal") or "").strip(),
            "backstory": (a.get("backstory") or "").strip(),
        }
        for a in agents
    ]
    snap_tasks = [
        {
            "id": (t.get("id") or "").strip(),
            "agent_id": (t.get("agent_id") or "").strip(),
            "description": (t.get("description") or "").strip(),
            "expected_output": (t.get("expected_output") or "").strip(),
        }
        for t in tasks
    ]
    return {"agents": snap_agents, "tasks": snap_tasks}


def _get_agents_tasks_from_state(config: dict, session_state) -> tuple[list, list]:
    """从 session_state 组装当前表单对应的 agents/tasks（与保存逻辑一致）。"""
    agents = config.get("agents") or []
    tasks = config.get("tasks") or []
    new_agents = []
    for i in range(len(agents)):
        a = agents[i]
        na = {k: v for k, v in a.items() if k not in ("id", "role", "goal", "backstory")}
        na["id"] = session_state.get(f"agent_id_{i}", a.get("id", ""))
        na["role"] = session_state.get(f"agent_role_{i}", a.get("role", ""))
        na["goal"] = session_state.get(f"agent_goal_{i}", a.get("goal", ""))
        na["backstory"] = (session_state.get(f"agent_back_{i}", "") or "").strip()
        new_agents.append(na)
    new_tasks = []
    for i in range(len(tasks)):
        t = tasks[i]
        nt = {k: v for k, v in t.items() if k not in ("id", "agent_id", "description", "expected_output")}
        nt["id"] = session_state.get(f"task_id_{i}", t.get("id", ""))
        nt["agent_id"] = session_state.get(f"task_agent_{i}", t.get("agent_id", ""))
        nt["description"] = (session_state.get(f"task_desc_{i}", "") or "").strip()
        nt["expected_output"] = session_state.get(f"task_out_{i}", t.get("expected_output", ""))
        new_tasks.append(nt)
    return new_agents, new_tasks


def _ensure_task_context(module_id: str) -> None:
    """初始化或更新 current_task_context，约定见 docs/ui-architecture-spec.md"""
    if "current_task_context" not in st.session_state:
        st.session_state["current_task_context"] = {
            "module_id": module_id,
            "task_id": "",
            "input_summary": {},
            "output_summary": {},
            "started_at": "",
            "status": "pending",
        }
    st.session_state["current_task_context"]["module_id"] = module_id


def _load_defaults():
    """从 Keyring/env/JSON/st.secrets 读取默认 Token / Key / 模型，线上优先 st.secrets。"""
    import json

    out: dict[str, str] = {"gemini_key": "", "gemini_model": "gemini-2.5-flash-lite"}

    # 1. 首选 credential_store（Keyring + 环境变量 + JSON 封装）
    try:
        from credential_store import get_credentials

        creds = get_credentials()
        for k in ("gemini_key", "gemini_model"):
            if creds.get(k):
                out[k] = str(creds[k])
    except ImportError:
        creds = {}

    # 2. 环境变量兜底
    out["gemini_key"] = out["gemini_key"] or os.getenv("GEMINI_API_KEY", "")
    env_model = os.getenv("GEMINI_MODEL", "")
    if env_model and not out["gemini_model"]:
        out["gemini_model"] = env_model

    # 3. 兼容旧版本的 config/defaults.json
    if os.path.isfile(DEFAULTS_PATH):
        try:
            with open(DEFAULTS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                out["gemini_key"] = out["gemini_key"] or str(data.get("gemini_key", "") or "")
                if not out["gemini_model"] and data.get("gemini_model"):
                    out["gemini_model"] = str(data.get("gemini_model") or "")
        except Exception:
            pass

    # 4. 线上优先 st.secrets（如在 Streamlit Cloud 等环境）
    try:
        import streamlit as st  # 本模块本身已依赖 streamlit

        secrets = getattr(st, "secrets", None)
        if secrets:
            if "gemini_key" in secrets and secrets["gemini_key"]:
                out["gemini_key"] = str(secrets["gemini_key"])
            if "gemini_model" in secrets and secrets["gemini_model"]:
                out["gemini_model"] = str(secrets["gemini_model"])
    except Exception:
        # 本地开发或无 secrets 时静默忽略
        pass

    if not out["gemini_model"]:
        out["gemini_model"] = "gemini-2.5-flash-lite"
    # 兼容已弃用模型：gemini-1.5-pro / gemini-1.5-flash 已于 2025-04 下架，自动映射到可用模型
    _DEPRECATED_MODEL_MAP = {"gemini-1.5-pro": "gemini-2.5-pro", "gemini-1.5-flash": "gemini-2.0-flash"}
    m = (out["gemini_model"] or "").strip().lower()
    if m in _DEPRECATED_MODEL_MAP:
        out["gemini_model"] = _DEPRECATED_MODEL_MAP[m]
    return out


def _save_defaults(gemini_key: str, gemini_model: str = "") -> str:
    """将默认 Key / 模型写入 Keyring 或 config/defaults.json。返回存储方式。"""
    try:
        from credential_store import set_credentials
        ok, mode = set_credentials(gemini_key, gemini_model)
        return mode
    except ImportError:
        pass
    import json
    os.makedirs(CONFIG_DIR, exist_ok=True)
    payload = {"gemini_key": gemini_key or ""}
    if gemini_model:
        payload["gemini_model"] = gemini_model
    with open(DEFAULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(DEFAULTS_PATH, 0o600)
    except OSError:
        pass
    return "JSON"


def _load_ui_texts():
    """从 config/ui_texts.yaml 加载文案，便于编辑。"""
    try:
        import yaml
        if os.path.isfile(UI_TEXTS_PATH):
            with open(UI_TEXTS_PATH, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    except Exception:
        pass
    return {}


def _get_text(data: dict, path: str, default: str = "") -> str:
    """从嵌套 dict 取文案，如 app.title。"""
    keys = path.split(".")
    for k in keys:
        if not isinstance(data, dict):
            return default
        data = data.get(k, {})
    return data if isinstance(data, str) else default


def _render_main_app(T: dict, cookies=None):
    """主应用：侧栏导航 + 主区内容。"""
    app_title = _get_text(T, "app.title") or "用例工坊 · AI 测试协作平台"
    defaults = _load_defaults()
    workbench_apps = _load_workbench_apps(T)

    # 初始化当前项目（双项目工作台）
    if "current_project" not in st.session_state:
        st.session_state["current_project"] = PROJECT_FAMBASE
    current_project = _get_current_project()

    # 设计系统 CSS（见 docs/implementation-handoff-for-programming.md）
    st.markdown("""
    <style>
    :root {
        --color-primary: #0d9488;
        --color-primary-dark: #0f766e;
        --color-primary-light: #5eead4;
        --color-bg-main: #f8fafc;
        --color-bg-card: #ffffff;
        --color-success: #059669;
        --color-error: #dc2626;
        --color-warning: #d97706;
        --radius-card: 12px;
        --radius-button: 10px;
    }
    .main .block-container {
        padding-top: 1.2rem; padding-bottom: 2.5rem;
        max-width: 960px; margin-left: auto; margin-right: auto;
    }
    .main { background: var(--color-bg-main); }
    h1 { font-size: 1.6rem !important; font-weight: 600 !important; color: #0f172a !important; margin-bottom: 0.6rem !important; letter-spacing: -0.02em; }
    h2 { font-size: 1.1rem !important; font-weight: 600 !important; color: #334155 !important; margin-top: 1.25rem !important; margin-bottom: 0.5rem !important; }
    h3 { font-size: 0.95rem !important; font-weight: 500 !important; color: #64748b !important; margin-top: 0.75rem !important; margin-bottom: 0.4rem !important; }
    p { line-height: 1.6; color: #334155; font-size: 0.95rem; }
    .step-label { font-size: 0.8rem; font-weight: 600; color: var(--color-primary); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.35rem; }
    .step-label.step-3, .step-label.step-4 { margin-top: 1.25rem; }
    div[data-testid="stExpander"] {
        margin-top: 0.5rem; margin-bottom: 0.25rem;
        border: 1px solid #e2e8f0; border-radius: var(--radius-card);
        background: var(--color-bg-card); box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    }
    div[data-testid="stExpander"] > div:first-child { border-radius: var(--radius-card); }
    .stButton > button { border-radius: var(--radius-button) !important; font-weight: 500 !important; transition: background 0.15s, color 0.15s; }
    .stButton > button:hover { opacity: 0.9; }
    .stButton > button[kind="primary"],
    .main .stButton > button[kind="primary"],
    [data-testid="stSidebar"] .stButton > button[kind="primary"] {
        font-weight: 600 !important; background: var(--color-primary) !important;
        color: #ffffff !important; border-color: var(--color-primary) !important;
    }
    .stButton > button[kind="primary"]:hover,
    .main .stButton > button[kind="primary"]:hover,
    [data-testid="stSidebar"] .stButton > button[kind="primary"]:hover {
        background: var(--color-primary-dark) !important; color: #ffffff !important; opacity: 1 !important;
    }
    .stTextInput > div > div, .stSelectbox > div { border-radius: var(--radius-card); }
    .stSuccess { border-radius: var(--radius-card); padding: 0.75rem 1rem; background: #ecfdf5; color: var(--color-success); }
    .stError { border-radius: var(--radius-card); padding: 0.75rem 1rem; color: var(--color-error); }
    .stWarning { border-radius: var(--radius-card); padding: 0.75rem 1rem; color: var(--color-warning); }
    .stInfo { border-radius: var(--radius-card); padding: 0.75rem 1rem; }
    [data-testid="stAlert"] { border-radius: var(--radius-card); }
    div[data-testid="stExpander"] summary { font-size: 0.9rem; padding: 0.6rem 0.75rem; }
    hr { margin: 1.25rem 0; border-color: #e2e8f0; opacity: 0.8; }
    .card-style { background: var(--color-bg-card); border-radius: var(--radius-card); box-shadow: 0 1px 3px rgba(0,0,0,0.08); padding: 1rem; margin-bottom: 1rem; }
    #MainMenu { visibility: hidden; }
    footer { visibility: hidden; }
    /* 不隐藏 header，否则会连同侧栏展开按钮一起隐藏，导致收起后无法打开 */
    </style>
    """, unsafe_allow_html=True)

    # 侧栏导航 + 项目切换器
    if "current_page" not in st.session_state:
        st.session_state["current_page"] = MODULE_RUN

    with st.sidebar:
        # 项目切换器
        st.markdown("**项目**")
        col_f, col_r = st.columns(2)
        with col_f:
            if st.button(
                "Fambase",
                key="project_btn_fambase",
                type="primary" if current_project == PROJECT_FAMBASE else "secondary",
            ):
                if current_project != PROJECT_FAMBASE:
                    if _has_unsaved_project_state():
                        st.session_state["project_switch_pending_target"] = PROJECT_FAMBASE
                    else:
                        _clear_project_related_state()
                        st.session_state["current_project"] = PROJECT_FAMBASE
                        _get_current_project()
                    st.rerun()
        with col_r:
            if st.button(
                "RM11",
                key="project_btn_rm11",
                type="primary" if current_project == PROJECT_RM11 else "secondary",
            ):
                if current_project != PROJECT_RM11:
                    if _has_unsaved_project_state():
                        st.session_state["project_switch_pending_target"] = PROJECT_RM11
                    else:
                        _clear_project_related_state()
                        st.session_state["current_project"] = PROJECT_RM11
                        _get_current_project()
                    st.rerun()

        pending_target = st.session_state.get("project_switch_pending_target")
        if pending_target:
            target_label = _get_project_display_name(pending_target)
            st.warning(f"切换到「{target_label}」将清空当前未保存内容，是否继续？")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("确认切换", key="project_switch_confirm"):
                    _clear_project_related_state()
                    st.session_state["current_project"] = _normalize_project_id(pending_target)
                    _get_current_project()
                    st.session_state["project_switch_pending_target"] = ""
                    st.rerun()
            with c2:
                if st.button("取消", key="project_switch_cancel"):
                    st.session_state["project_switch_pending_target"] = ""
                    st.rerun()

        st.caption(f"当前项目：{_get_project_display_name(current_project)}")
        st.divider()

        st.markdown(f"**{app_title}**")
        st.caption(_get_text(T, "app.slogan") or "需求即输入，用例即输出，AI 全程协同一键生成")
        st.divider()
        st.markdown("**工作台**")
        for app in workbench_apps:
            module_id = app["id"]
            if module_id in (MODULE_RUN, MODULE_RISK_REPORT, MODULE_MEMORY, MODULE_CHAT):
                if st.button(
                    app["label"],
                    key=f"nav_{module_id}",
                    use_container_width=True,
                    type="primary" if st.session_state["current_page"] == module_id else "secondary",
                ):
                    st.session_state["current_page"] = module_id
                    st.rerun()
        st.markdown("**高级**")
        for app in workbench_apps:
            module_id = app["id"]
            if module_id in (MODULE_AGENTS, MODULE_SETTINGS):
                if st.button(
                    app["label"],
                    key=f"nav_{module_id}",
                    use_container_width=True,
                    type="primary" if st.session_state["current_page"] == module_id else "secondary",
                ):
                    st.session_state["current_page"] = module_id
                    st.rerun()
        st.divider()
        # 版本号：便于验证线上代码已成功更新
        ver_info = _load_version()
        ver_str = str(ver_info.get("version", "") or "").strip()
        build_str = str(ver_info.get("build_time", "") or "").strip()
        if ver_str:
            ver_label = _get_text(T, "app.version_label") or "版本"
            ver_display = f"{ver_label}: {ver_str}"
            if build_str:
                ver_display += f" ({build_str})"
            st.caption(ver_display)

    # 主区内容
    current_page = st.session_state["current_page"]
    _ensure_task_context(current_page)

    _page_labels = {a["id"]: a["label"] for a in workbench_apps}
    _page_title = _page_labels.get(current_page, current_page)

    if current_page == MODULE_RUN:
        st.title(_get_text(T, "run_tab.run_btn") or "生成测试用例")
    else:
        st.title(_page_title)
    # 页面级当前项目提示
    st.caption(f"当前项目：{_get_project_display_name(_get_current_project())}")

    if current_page == MODULE_RUN:
        _render_module_run(T, defaults)
    elif current_page == MODULE_RISK_REPORT:
        _render_module_risk_report(T, defaults)
    elif current_page == MODULE_AGENTS:
        _render_module_agents(T)
    elif current_page == MODULE_MEMORY:
        _render_module_memory(T, defaults)
    elif current_page == MODULE_CHAT:
        _render_module_chat(T, defaults)
    elif current_page == MODULE_SETTINGS:
        _render_module_settings(T, defaults)
    else:
        st.info(f"模块「{current_page}」尚未实现。")


def _render_module_run(T: dict, defaults: dict):
    """工作台模块：上传 / 粘贴 → 四 Agent → 测试用例。"""
    st.caption(_get_text(T, "run_tab.page_caption") or "从需求文档生成测试用例，支持上传或粘贴，导出 Excel。")
    st.markdown("<div style='margin-bottom:0.5rem'></div>", unsafe_allow_html=True)

    demand_source = st.radio(
        "需求来源",
        options=["upload", "paste"],
        format_func=lambda x: {
            "upload": _get_text(T, "run_tab.demand_source_upload") or "上传文件",
            "paste": _get_text(T, "run_tab.demand_source_paste") or "粘贴文本",
        }.get(x, x),
        key="run_demand_source",
        horizontal=True,
    )

    # F5-2 配置状态提示
    _has_key = bool(defaults.get("gemini_key"))
    _config_status = (_get_text(T, "run_tab.config_status_ok") or "Token/Key 已配置") if _has_key else (_get_text(T, "run_tab.config_status_missing") or "请在「设置」中配置 Token 与 Gemini API Key")
    st.caption(f"📌 {_config_status}")

    if demand_source == "upload":
        _render_upload_mode(T, defaults)
        _render_run_history(T)
        return
    _render_paste_mode(T, defaults)
    _render_run_history(T)


def _render_run_history(T: dict) -> None:
    """历史记录：按关键字过滤、卡片列表、删除（二次确认）、下载 Excel。F4 + F5-3"""
    st.divider()
    st.subheader(_get_text(T, "run_tab.history_section") or "历史记录")

    _delete_confirm_key = _get_module_state_key(MODULE_RUN, "delete_confirm_id")
    keyword = st.text_input(
        "搜索",
        key="run_history_filter",
        placeholder=_get_text(T, "run_tab.history_filter_placeholder") or "按标题或来源类型搜索…",
        label_visibility="collapsed",
    )
    keyword = (keyword or "").strip()

    try:
        from run_history import list_run_records, delete_run_record, get_full_result, get_excel_filename
    except ImportError:
        st.caption("历史记录模块未就绪")
        return

    records = list_run_records(
        keyword=keyword or "",
        limit=20,
        project_id=_get_current_project(),
    )
    if not records:
        st.info(_get_text(T, "run_tab.history_empty_state") or "暂无生成记录，上传或粘贴需求后开始生成")
        return

    for rec in records:
        stype = rec.get("source_type") or ""
        title = (rec.get("demand_title") or "")[:50]
        ts = rec.get("timestamp", "")
        card_title = f"{stype} · {title}"
        rid = rec.get("id", "")

        with st.container():
            cols = st.columns([4, 1])
            with cols[0]:
                st.markdown(f"**{card_title}**  ·  {ts}")
            with cols[1]:
                if st.button("🗑️", key=f"run_del_{rid}", help=_get_text(T, "run_tab.delete_btn") or "删除"):
                    st.session_state[_delete_confirm_key] = rid
                    st.rerun()

        if st.session_state.get(_delete_confirm_key) == rid:
            st.warning(_get_text(T, "run_tab.delete_confirm_msg") or "确定要删除这条历史记录吗？")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("确认删除", key=f"run_del_ok_{rid}"):
                    delete_run_record(rid)
                    st.session_state[_delete_confirm_key] = None
                    st.rerun()
            with c2:
                if st.button("取消", key=f"run_del_cancel_{rid}"):
                    st.session_state[_delete_confirm_key] = None
                    st.rerun()
            st.divider()
            continue

        ex_path = rec.get("excel_path") or ""
        if ex_path and os.path.isfile(ex_path):
            fn = get_excel_filename(rec)
            with open(ex_path, "rb") as f:
                st.download_button(
                    _get_text(T, "run_tab.download_excel") or "📥 下载 Excel",
                    data=f.read(),
                    file_name=fn,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key=f"run_dl_{rid}",
                )
        else:
            st.caption(_get_text(T, "run_tab.no_excel_hint") or "本次无可下载表格")

        with st.expander("查看详情", expanded=False):
            full_text = get_full_result(rec, extra_allowed_dirs=[_get_output_dir()])
            st.markdown(full_text or "*（无）*")

        st.divider()


def _render_paste_mode(T: dict, defaults: dict):
    """粘贴文本模式：大文本框粘贴 PRD，可选 .xlsx 既有用例，跑四 Agent。"""
    st.caption(_get_text(T, "run_tab.paste_xlsx_hint") or "可同时上传 .xlsx 作为既有用例上下文（可选）")

    pasted = st.text_area(
        "PRD 内容",
        height=220,
        key="run_paste_content",
        placeholder=_get_text(T, "run_tab.paste_placeholder") or "在此粘贴 PRD 或需求文档内容…",
        label_visibility="collapsed",
    ).strip()

    xlsx_uploaded = st.file_uploader(
        "可选：上传 .xlsx 既有用例",
        type=["xlsx"],
        accept_multiple_files=False,
        key="run_paste_xlsx",
        help="可选；上传后作为 Agent 上下文",
    )

    # 缓存粘贴模式下上传的 xlsx，切换 Tab / 模式后仍保留
    xlsx_cache_key = "run_paste_xlsx_cache"
    if xlsx_uploaded:
        try:
            name = getattr(xlsx_uploaded, "name", "") or "既有用例.xlsx"
            data = xlsx_uploaded.read()
            st.session_state[xlsx_cache_key] = {"name": name, "bytes": data}
        except Exception:
            pass

    _paste_key = _get_module_state_key(MODULE_RUN, "paste_running")
    _paste_result_key = _get_module_state_key(MODULE_RUN, "paste_last_run")
    _paste_error_key = _get_module_state_key(MODULE_RUN, "paste_last_error")

    # 用户主动清空时才重置缓存，满足 F6-2 约束
    if st.button("清空文本与附件", key="run_paste_reset"):
        st.session_state["run_paste_content"] = ""
        st.session_state["run_paste_xlsx"] = None
        st.session_state.pop(xlsx_cache_key, None)
        st.session_state[_paste_result_key] = None
        st.session_state[_paste_error_key] = None
        st.rerun()

    existing_cases = ""
    cached_xlsx = st.session_state.get(xlsx_cache_key)
    if xlsx_uploaded or cached_xlsx:
        try:
            files_for_parse = []
            if xlsx_uploaded:
                files_for_parse = [xlsx_uploaded]
            elif cached_xlsx:
                files_for_parse = [
                    _MemoryUpload(cached_xlsx["name"], cached_xlsx["bytes"])
                ]
            _, existing_cases, _ = parse_uploaded_files(files_for_parse)
        except Exception:
            pass

    gemini_models_list, default_model = _load_models()
    _model_opts = [m[0] for m in gemini_models_list]
    _model_idx = next(
        (i for i, (k, _) in enumerate(gemini_models_list) if k == (defaults.get("gemini_model") or default_model)),
        0,
    )
    with st.expander("模型配置", expanded=not bool(defaults.get("gemini_key"))):
        gemini_model = st.selectbox(
            "Gemini 模型",
            options=_model_opts,
            index=_model_idx,
            format_func=lambda x: dict(gemini_models_list).get(x, x),
            key="run_paste_model",
        )

    pipeline_running = st.session_state.get(_paste_key, False)
    run_label = "运行中…" if pipeline_running else "开始生成"
    if st.button(run_label, type="primary", use_container_width=True, key="run_paste_btn", disabled=pipeline_running):
        if not pasted:
            st.error("请粘贴需求文档内容")
        elif not defaults.get("gemini_key"):
            st.error("请先在「设置」中配置 Gemini API Key")
        else:
            st.session_state[_paste_error_key] = None
            st.session_state[_paste_key] = True
            _ph = st.empty()
            try:
                with _ph.container():
                    st.progress(0.3, text="分析文档并生成用例…")
                with st.spinner("正在生成…"):
                    result = run_upload_to_cases(
                        demand_md=pasted,
                        existing_cases=existing_cases,
                        gemini_key=defaults.get("gemini_key", ""),
                        gemini_model=gemini_model,
                        project_context=get_project_context_for_agent(),
                    )
                if not result["ok"]:
                    st.session_state[_paste_error_key] = result.get("error", "执行失败")
                    st.error(result.get("error", "执行失败"))
                else:
                    st.session_state[_paste_result_key] = result
                    st.session_state[_paste_error_key] = None
                    _ph.progress(1.0, text="完成")
                    st.success("生成完成")
                    # 标题：首行或前 20 字
                    _lines = pasted.splitlines()
                    _first = (_lines[0] if _lines else "").strip()
                    _demand_title = (_first[:20] + "…") if len(_first) > 20 else (_first or "粘贴需求")
                    _result_str = f"## 1. 理解内容\n\n{str(result.get('understanding') or '')}\n\n## 2. 问题点\n\n{str(result.get('issues') or '')}\n\n## 3. 新用例表\n\n{str(result.get('cases_md') or '')}"
                    _ex_path = None
                    _txt_path = None
                    try:
                        from run_history import add_run_record, slug_for_filename
                        from datetime import datetime
                        _out = _get_output_dir()
                        _rid = datetime.now().strftime("%Y%m%d_%H%M%S")
                        _slug = slug_for_filename(_demand_title, 20)
                        if result.get("excel_bytes"):
                            _ex_path = os.path.join(_out, f"测试用例_{_slug}_{_rid}.xlsx")
                            os.makedirs(_out, exist_ok=True)
                            with open(_ex_path, "wb") as f:
                                f.write(result["excel_bytes"])
                        _txt_path = os.path.join(_out, f"run_{_rid}.txt")
                        with open(_txt_path, "w", encoding="utf-8") as f:
                            f.write(_result_str)
                        add_run_record(
                            source_type="粘贴",
                            demand_title=_demand_title,
                            result_str=_result_str,
                            excel_path=_ex_path,
                            txt_path=_txt_path,
                            project_id=_get_current_project(),
                        )
                    except Exception:
                        pass
                    # 更新当前用例状态（供增量生成使用）
                    try:
                        _update_cases_state_from_md(pasted, str(result.get("cases_md") or ""))
                    except Exception:
                        pass
            except Exception as e:
                st.session_state[_paste_error_key] = str(e)
                st.error(str(e))
            finally:
                _ph.empty()
                st.session_state[_paste_key] = False

    _last_err = st.session_state.get(_paste_error_key)
    if _last_err and not pipeline_running:
        st.error(_last_err)
        if st.button("重试", key="run_paste_retry"):
            st.session_state[_paste_error_key] = None
            st.rerun()

    r = st.session_state.get(_paste_result_key)
    if not r:
        st.info("粘贴需求内容后点击「开始生成」。仅展示新测试用例表与 Excel 下载。")
        return

    st.divider()
    st.subheader("新测试用例表")
    st.markdown(r.get("cases_md", "") or "*（无）*")

    excel_bytes = st.session_state.get("current_cases_excel_bytes") or r.get("excel_bytes")
    if excel_bytes:
        st.download_button(
            "📥 下载 Excel",
            data=excel_bytes,
            file_name="测试用例.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="dl_paste_excel",
        )
    else:
        st.caption("未解析到 Markdown 表格，无法导出 Excel。")

    # 用例补充（增量生成）
    _render_incremental_section(T, defaults)

    # 用例补充（增量生成）
    _render_incremental_section(T, defaults)


def _render_upload_mode(T: dict, defaults: dict):
    """文件上传模式：上传 .md / .docx（需求文档）+ .xlsx（既有用例）→ 解析 → 四 Agent → 三块结果 + Excel 下载。
    仅 Excel 下载，不配置导出路径或 Sheets。"""
    st.caption("支持 .md / .docx（需求文档）和 .xlsx（既有测试用例），可混合选择。至少需 1 个需求文档。")

    uploaded = st.file_uploader(
        "上传需求文档与既有用例",
        type=["md", "docx", "xlsx"],
        accept_multiple_files=True,
        key="run_upload_files",
        help="支持 .md、.docx（Word）、.xlsx，可混合选择；单文件 &lt; 10MB，总 &lt; 50MB",
    )

    # 将上传文件缓存到 session_state，切换 Tab 或 demand_source 后仍可使用
    cache_key = "run_upload_files_cache"
    if uploaded:
        files = uploaded if isinstance(uploaded, list) else [uploaded]
        cached = []
        for f in files:
            try:
                name = getattr(f, "name", "") or "未命名"
                data = f.read()
            except Exception:
                continue
            cached.append({"name": name, "bytes": data})
        if cached:
            st.session_state[cache_key] = cached
    cached_files = st.session_state.get(cache_key) or []
    effective_files = [
        _MemoryUpload(item["name"], item["bytes"]) for item in cached_files
    ]

    # 用户主动清空时才重置缓存，满足 F6-2 约束
    _upload_key = _get_module_state_key(MODULE_RUN, "upload_running")
    _upload_result_key = _get_module_state_key(MODULE_RUN, "upload_last_run")
    _upload_error_key = _get_module_state_key(MODULE_RUN, "upload_last_error")
    if st.button("清空已选文件", key="run_upload_reset"):
        st.session_state["run_upload_files"] = None
        st.session_state[_upload_result_key] = None
        st.session_state[_upload_error_key] = None
        st.session_state.pop("run_upload_files_cache", None)
        st.rerun()

    demand_md = ""
    existing_cases = ""
    preview_infos = []

    try:
        if effective_files:
            demand_md, existing_cases, preview_infos = parse_uploaded_files(effective_files)
    except Exception as e:
        st.error(f"解析失败：{e}")
    else:
        for p in preview_infos:
            name = p.get("name", "")
            if p.get("type") in ("md", "docx"):
                prev = p.get("preview", "")[:200]
                st.caption(
                    f"📄 {name} — {prev}…"
                    if len(str(p.get("preview", ""))) > 200
                    else f"📄 {name} — {prev}"
                )
            else:
                st.caption(f"📊 {name} — {p.get('rows', 0)} 行")

    if not demand_md and effective_files:
        st.warning("至少需上传 1 个需求文档（.md 或 .docx）；或文件类型/大小不符要求。")

    gemini_models_list, default_model = _load_models()
    _model_opts = [m[0] for m in gemini_models_list]
    _model_idx = next((i for i, (k, _) in enumerate(gemini_models_list) if k == (defaults.get("gemini_model") or default_model)), 0)
    with st.expander("模型配置", expanded=not bool(defaults.get("gemini_key"))):
        gemini_model = st.selectbox(
            "Gemini 模型",
            options=_model_opts,
            index=_model_idx,
            format_func=lambda x: dict(gemini_models_list).get(x, x),
            key="run_upload_model",
        )
    st.caption("仅支持 Excel 下载，不配置导出路径。")

    pipeline_running = st.session_state.get(_upload_key, False)
    run_label = "运行中…" if pipeline_running else "开始生成"
    if st.button(run_label, type="primary", use_container_width=True, key="run_upload_btn", disabled=pipeline_running):
        if not demand_md:
            st.error("请至少上传 1 个需求文档（.md 或 .docx）")
        elif not defaults.get("gemini_key"):
            st.error("请先在「设置」中配置 Gemini API Key")
        else:
            st.session_state[_upload_error_key] = None
            st.session_state[_upload_key] = True
            _ph = st.empty()
            try:
                with _ph.container():
                    st.progress(0.3, text="分析文档并生成用例…")
                with st.spinner("正在生成…"):
                    result = run_upload_to_cases(
                        demand_md=demand_md,
                        existing_cases=existing_cases,
                        gemini_key=defaults.get("gemini_key", ""),
                        gemini_model=gemini_model,
                        project_context=get_project_context_for_agent(),
                    )
                if not result["ok"]:
                    st.session_state[_upload_error_key] = result.get("error", "执行失败")
                    st.error(result.get("error", "执行失败"))
                else:
                    st.session_state[_upload_result_key] = result
                    st.session_state[_upload_error_key] = None
                    _ph.progress(1.0, text="完成")
                    st.success("生成完成")
                    # 写入历史
                    _demand_title = "上传需求"
                    for p in preview_infos:
                        if p.get("type") in ("md", "docx"):
                            _demand_title = os.path.splitext(p.get("name", ""))[0] or _demand_title
                            break
                    _result_str = f"## 1. 理解内容\n\n{str(result.get('understanding') or '')}\n\n## 2. 问题点\n\n{str(result.get('issues') or '')}\n\n## 3. 新用例表\n\n{str(result.get('cases_md') or '')}"
                    _ex_path = None
                    _txt_path = None
                    try:
                        from run_history import add_run_record, slug_for_filename
                        from datetime import datetime
                        _out = _get_output_dir()
                        _rid = datetime.now().strftime("%Y%m%d_%H%M%S")
                        _slug = slug_for_filename(_demand_title, 20)
                        if result.get("excel_bytes"):
                            _ex_path = os.path.join(_out, f"测试用例_{_slug}_{_rid}.xlsx")
                            os.makedirs(_out, exist_ok=True)
                            with open(_ex_path, "wb") as f:
                                f.write(result["excel_bytes"])
                        _txt_path = os.path.join(_out, f"run_{_rid}.txt")
                        with open(_txt_path, "w", encoding="utf-8") as f:
                            f.write(_result_str)
                        add_run_record(
                            source_type="上传",
                            demand_title=_demand_title,
                            result_str=_result_str,
                            excel_path=_ex_path,
                            txt_path=_txt_path,
                            project_id=_get_current_project(),
                        )
                    except Exception:
                        pass
                    # 更新当前用例状态（供增量生成使用）
                    try:
                        _update_cases_state_from_md(demand_md, str(result.get("cases_md") or ""))
                    except Exception:
                        pass
            except Exception as e:
                st.session_state[_upload_error_key] = str(e)
                st.error(str(e))
            finally:
                _ph.empty()
                st.session_state[_upload_key] = False

    _last_err = st.session_state.get(_upload_error_key)
    if _last_err and not pipeline_running:
        st.error(_last_err)
        if st.button("重试", key="run_upload_retry"):
            st.session_state[_upload_error_key] = None
            st.rerun()

    r = st.session_state.get(_upload_result_key)
    if not r:
        st.info("上传 .md/.docx 需求文档或 .xlsx 既有用例后点击「开始生成」。仅展示新测试用例表与 Excel 下载。")
        return

    st.divider()
    st.subheader("新测试用例表")
    st.markdown(r.get("cases_md", "") or "*（无）*")

    excel_bytes = st.session_state.get("current_cases_excel_bytes") or r.get("excel_bytes")
    if excel_bytes:
        st.download_button(
            "📥 下载 Excel",
            data=excel_bytes,
            file_name="测试用例.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="dl_upload_excel",
        )
    else:
        st.caption("未解析到 Markdown 表格，无法导出 Excel。")


def _render_module_risk_report(T: dict, defaults: dict):
    """工作台模块：需求风险分析。独立调用分析 Agent，不参与四 Agent 协作。"""
    st.subheader(_get_text(T, "risk_report.section_title") or "需求风险分析")
    st.caption(_get_text(T, "risk_report.section_desc") or "单独对文档做风险评估，产出表格报告，不参与四 Agent 流程。")

    source_options = ["paste", "memory"] if MEMORY_AVAILABLE else ["paste"]
    doc_source = st.radio(
        _get_text(T, "risk_report.doc_source") or "文档来源",
        options=source_options,
        format_func=lambda x: {
            "paste": _get_text(T, "risk_report.doc_source_paste") or "粘贴内容",
            "memory": _get_text(T, "risk_report.doc_source_memory") or "项目记忆（选择近期文档）",
        }[x],
        key="risk_report_doc_source",
    )

    doc_context = ""
    if doc_source == "paste":
        doc_context = st.text_area(
            _get_text(T, "chat_tab.paste_placeholder") or "粘贴需求文档内容",
            height=180,
            key="risk_report_paste",
            label_visibility="collapsed",
        ).strip()
    else:
        entries = list_recent(limit=20, project_id=_get_current_project())
        if not entries:
            st.info(_get_text(T, "chat_tab.doc_source_empty") or "项目记忆暂无需求文档，请先在「项目记忆」页导入。")
        else:
            options = [f"{e.get('title', '') or e.get('source_id', '未命名')} ({e.get('created_at', '')})" for e in entries]
            sel = st.selectbox(
                _get_text(T, "risk_report.doc_source_memory") or "选择近期文档",
                options=options,
                key="risk_report_memory_sel",
            )
            if sel and sel in options:
                idx = options.index(sel)
                doc_context = (entries[idx].get("content") or entries[idx].get("summary") or "").strip()

    running_key = _get_module_state_key(MODULE_RISK_REPORT, "running")
    result_key = _get_module_state_key(MODULE_RISK_REPORT, "result")
    error_key = _get_module_state_key(MODULE_RISK_REPORT, "error")
    excel_path_key = _get_module_state_key(MODULE_RISK_REPORT, "excel_path")

    is_running = st.session_state.get(running_key, False)
    run_btn_label = (_get_text(T, "risk_report.run_btn") or "生成风险报告") if not is_running else (_get_text(T, "run_tab.run_spinner") or "运行中…")
    if st.button(run_btn_label, type="primary", key="risk_report_run_btn", disabled=is_running):
        if not doc_context or not doc_context.strip():
            st.warning(_get_text(T, "risk_report.empty_doc_warning") or "请先输入或拉取文档内容")
        else:
            st.session_state[error_key] = None
            st.session_state[running_key] = True
            try:
                os.environ["GEMINI_API_KEY"] = defaults.get("gemini_key") or os.environ.get("GEMINI_API_KEY", "")
                os.environ["GEMINI_MODEL"] = defaults.get("gemini_model") or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
                if not os.environ.get("GEMINI_API_KEY"):
                    st.session_state[error_key] = _get_text(T, "run_tab.gemini_required") or "请填写 Gemini API Key"
                else:
                    with st.spinner(_get_text(T, "run_tab.run_spinner") or "正在分析…"):
                        result = generate_risk_assessment_report(
                            document_content=doc_context,
                            gemini_model=defaults.get("gemini_model", ""),
                            gemini_key=defaults.get("gemini_key", ""),
                        )
                    st.session_state[result_key] = result
                    st.session_state[error_key] = None
                    tables = _parse_markdown_tables(result)
                    if tables:
                        from datetime import datetime
                        os.makedirs(OUTPUT_DIR, exist_ok=True)
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        excel_path = os.path.join(OUTPUT_DIR, f"risk_report_{ts}.xlsx")
                        if _export_to_excel(tables, excel_path):
                            st.session_state[excel_path_key] = excel_path
                        else:
                            st.session_state[excel_path_key] = None
                    else:
                        st.session_state[excel_path_key] = None
            except ValueError as e:
                st.session_state[error_key] = str(e)
            except Exception as e:
                err_msg = str(e)
                if "timeout" in err_msg.lower() or "429" in err_msg or "503" in err_msg:
                    st.session_state[error_key] = _get_text(T, "risk_report.timeout_error") or "分析超时，请稍后重试"
                else:
                    st.session_state[error_key] = err_msg
            finally:
                st.session_state[running_key] = False
            st.rerun()

    last_error = st.session_state.get(error_key)
    if last_error and not st.session_state.get(running_key, False):
        st.error(last_error)
        if st.button(_get_text(T, "run_tab.retry_btn") or "重试", key="risk_report_retry"):
            st.session_state[error_key] = None
            st.rerun()

    last_result = st.session_state.get(result_key)
    if last_result:
        tables = _parse_markdown_tables(last_result)
        if not tables:
            st.warning(_get_text(T, "risk_report.parse_warning") or "未能解析为表格，展示原始输出")
        st.markdown(last_result)
        excel_path = st.session_state.get(excel_path_key)
        if excel_path and os.path.isfile(excel_path):
            with open(excel_path, "rb") as f:
                st.download_button(
                    _get_text(T, "risk_report.download_excel") or "📥 导出 Excel",
                    f,
                    file_name=os.path.basename(excel_path),
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="risk_report_dl_excel",
                )


def _render_module_agents(T: dict):
    """工作台模块：编辑 Agent。"""
    _agents_dirty = st.session_state.get(_get_module_state_key(MODULE_AGENTS, "dirty"), False)
    if _agents_dirty:
        st.warning("您有未保存的修改，请点击底部「保存配置」后再切换页面。")
    st.subheader(_get_text(T, "agents_tab.section_title") or "编辑 Agent 与 Task")
    st.caption(_get_text(T, "agents_tab.section_caption") or "修改角色、目标与任务描述后，点击底部「保存配置」生效；下次生成用例将使用新配置。")
    config = load_agents_config()
    if not config:
        st.warning("未找到 config/agents.yaml 或 PyYAML 未安装；可在此编辑并保存。")
        raw_yaml = st.text_area("agents.yaml 内容", height=400, placeholder="agents:\n  - id: ...\n    role: ...\n    goal: ...\n    backstory: |\n      ...", key="agents_raw_yaml")
        if st.button("保存 agents.yaml", key="agents_save_raw"):
            if raw_yaml.strip():
                os.makedirs(CONFIG_DIR, exist_ok=True)
                with open(AGENTS_CONFIG_PATH, "w", encoding="utf-8") as f:
                    f.write(raw_yaml.strip())
                st.success("已保存")
                st.rerun()
    else:
        agents = config.get("agents") or []
        tasks = config.get("tasks") or []
        st.markdown("**Agent（角色）**")
        for i, a in enumerate(agents):
            with st.expander(f"Agent {i + 1}/{len(agents)}: {a.get('role', a.get('id', '未命名'))}", expanded=False):
                st.text_input("id", value=a.get("id", ""), key=f"agent_id_{i}", help="唯一标识，Task 中通过 agent_id 引用")
                st.text_input("role", value=a.get("role", ""), key=f"agent_role_{i}")
                st.text_area("goal", value=a.get("goal", ""), key=f"agent_goal_{i}", height=80)
                st.text_area("backstory", value=(a.get("backstory") or "").strip(), key=f"agent_back_{i}", height=120)
        st.divider()
        st.markdown("**Task（任务）**")
        for i, t in enumerate(tasks):
            with st.expander(f"Task {i + 1}/{len(tasks)}: {t.get('id', '')} ← {t.get('agent_id', '')}", expanded=False):
                st.text_input("id", value=t.get("id", ""), key=f"task_id_{i}")
                st.text_input("agent_id", value=t.get("agent_id", ""), key=f"task_agent_{i}", help="对应上方某 Agent 的 id")
                st.text_area("description", value=(t.get("description") or "").strip(), key=f"task_desc_{i}", height=100)
                st.text_input("expected_output", value=t.get("expected_output", ""), key=f"task_out_{i}")
        _last_hash_key = _get_module_state_key(MODULE_AGENTS, "last_saved_hash")
        new_agents, new_tasks = _get_agents_tasks_from_state(config, st.session_state)
        snapshot_now = _build_agents_snapshot(new_agents, new_tasks)
        _snapshot_str = json.dumps(snapshot_now, ensure_ascii=False, sort_keys=True)
        if _last_hash_key not in st.session_state:
            st.session_state[_last_hash_key] = json.dumps(
                _build_agents_snapshot(agents, tasks), ensure_ascii=False, sort_keys=True
            )
        if st.session_state[_last_hash_key] != _snapshot_str:
            st.session_state[_get_module_state_key(MODULE_AGENTS, "dirty")] = True
        else:
            st.session_state[_get_module_state_key(MODULE_AGENTS, "dirty")] = False
        if st.button(_get_text(T, "agents_tab.save_btn") or "保存配置到 config/agents.yaml", type="primary", key="agents_save_config"):
            snapshot_expected_str = _snapshot_str
            last_saved_str = st.session_state.get(_last_hash_key, "")
            if last_saved_str and snapshot_expected_str == last_saved_str:
                st.info(_get_text(T, "common.save_no_change") or "内容未变更，无需保存")
            else:
                try:
                    import yaml
                    to_save = {k: v for k, v in config.items() if k not in ("agents", "tasks")}
                    to_save["agents"] = new_agents
                    to_save["tasks"] = new_tasks
                    with open(AGENTS_CONFIG_PATH, "w", encoding="utf-8") as f:
                        yaml.dump(to_save, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
                    saved = load_agents_config()
                    snap_saved = _build_agents_snapshot(saved.get("agents", []), saved.get("tasks", []))
                    snap_expected = _build_agents_snapshot(new_agents, new_tasks)
                    if json.dumps(snap_saved, ensure_ascii=False, sort_keys=True) == json.dumps(snap_expected, ensure_ascii=False, sort_keys=True):
                        st.session_state[_last_hash_key] = json.dumps(snap_saved, ensure_ascii=False, sort_keys=True)
                        st.session_state[_get_module_state_key(MODULE_AGENTS, "dirty")] = False
                        st.success(_get_text(T, "agents_tab.save_success") or "已保存，下次生成用例将使用新配置")
                        st.rerun()
                    else:
                        st.error(_get_text(T, "agents_tab.save_fail_mismatch") or "保存失败：写入内容与预期不一致，请刷新页面后重试")
                except Exception as e:
                    st.error(
                        (_get_text(T, "agents_tab.save_fail_io") or "保存失败：无法写入配置文件，请检查磁盘权限或稍后重试")
                        + f"（{e}）"
                    )

def _render_module_memory(T: dict, defaults: dict):
    """工作台模块：项目记忆。"""
    st.subheader(_get_text(T, "memory_tab.section_title") or "项目记忆")
    st.caption(_get_text(T, "memory_tab.caption_browse") or "导入的需求文档供 Agent 参考；先搜索查看已有内容，再按需导入。")

    project_id = _get_current_project()

    if not MEMORY_AVAILABLE:
        st.info("当前运行环境未启用项目记忆存储（例如 sqlite3 不可用），项目记忆功能暂不可用，但生成用例功能不受影响。")
        return

    # Agent 知识库区块
    try:
        from agent_knowledge_service import (
            build_agent_knowledge,
            get_last_updated,
            is_knowledge_stale,
        )
        kb_section = _get_text(T, "memory_tab.knowledge_section") or "Agent 知识库"
        st.markdown(f"**{kb_section}**")
        last_updated = get_last_updated(project_id=project_id)
        last_text = (
            (_get_text(T, "memory_tab.knowledge_last_updated") or "知识库最后更新时间：{time}").replace("{time}", last_updated)
            if last_updated
            else (_get_text(T, "memory_tab.knowledge_not_generated") or "尚未生成")
        )
        st.caption(last_text)

        # 自动更新：进入页面且知识库过期时触发一次
        auto_done_key = _get_module_state_key(MODULE_MEMORY, "kb_auto_done")
        if is_knowledge_stale(project_id=project_id) and not st.session_state.get(auto_done_key, False):
            st.session_state[auto_done_key] = True
            refresh_label = _get_text(T, "memory_tab.knowledge_auto_updating") or "知识库已过期，正在自动更新…"
            with st.spinner(refresh_label):
                ok, err = build_agent_knowledge(
                    gemini_key=defaults.get("gemini_key", ""),
                    gemini_model=defaults.get("gemini_model", ""),
                )
                if ok:
                    st.rerun()
                # 失败则静默，不阻塞
        elif is_knowledge_stale(project_id=project_id):
            st.info(_get_text(T, "memory_tab.knowledge_stale_warning") or "知识库已超过 7 天未更新，建议点击【刷新知识库】或等待自动更新。")

        if st.button(_get_text(T, "memory_tab.knowledge_refresh_btn") or "刷新知识库", key="kb_refresh"):
            with st.spinner(_get_text(T, "memory_tab.knowledge_auto_updating") or "知识库已过期，正在自动更新…"):
                ok, err = build_agent_knowledge(
                    gemini_key=defaults.get("gemini_key", ""),
                    gemini_model=defaults.get("gemini_model", ""),
                )
                if ok:
                    st.success(_get_text(T, "memory_tab.knowledge_refresh_success") or "知识库已更新")
                    st.rerun()
                else:
                    fail_tpl = _get_text(T, "memory_tab.knowledge_refresh_fail") or "更新失败：{err}"
                    st.error(fail_tpl.replace("{err}", err))
        st.divider()
    except ImportError:
        pass

    st.markdown("**搜索**")
    kw = st.text_input(
        _get_text(T, "memory_tab.search_label") or "搜索",
        placeholder=_get_text(T, "memory_tab.search_placeholder") or "输入关键词（如：直播、禁言、AB test）",
        key="mem_search",
        label_visibility="collapsed",
    )
    entries = search(kw, limit=20, project_id=project_id) if kw and kw.strip() else []
    if entries:
        for e in entries:
            label = f"【{e.get('source_type', '')}】{e.get('title', '') or e.get('source_id', '')} — {e.get('created_at', '')}"
            col_title, col_del = st.columns([1, 0.12])
            with col_title:
                with st.expander(label, expanded=False):
                    content = e.get("content", "") or e.get("summary", "")
                    sid = e.get("source_id", "")
                    src_type = e.get("source_type", "")
                    if src_type == TEST_CASES_SOURCE_TYPE:
                        src = "导入（Excel/CSV/粘贴）"
                    else:
                        src = sid or "-"
                    st.caption(f"来源: {src}")
                    st.markdown(content[:2000] + ("..." if len(content) > 2000 else ""))
            with col_del:
                if st.button("🗑", key=f"del_{e.get('id')}", type="secondary", help="删除此条"):
                    delete_entry(e.get("id"))
                    st.rerun()
    elif kw and kw.strip():
        st.info(_get_text(T, "memory_tab.search_empty") or "未找到匹配文档。")
    else:
        st.info(_get_text(T, "memory_tab.search_first") or "输入关键词搜索，或通过下方导入后搜索。")

    st.markdown("**" + (_get_text(T, "memory_tab.history_timeline_title") or (_get_text(T, "memory_tab.import_history_section") or "导入历史")) + "**")
    hist = list_import_history(limit=20, project_id=project_id)
    if hist:
        pending_entries = [
            e for e in hist if (e.get("agent_summary_status") or "pending") in ("pending", "failed")
        ]
        if pending_entries:
            if st.button(
                f"批量生成缺失摘要（{len(pending_entries)} 条）",
                key="batch_gen_summary",
                type="secondary",
            ):
                gemini_key = defaults.get("gemini_key", "")
                success_count = 0
                fail_count = 0
                prog = st.progress(0.0, text="批量生成摘要中…")
                for idx, e in enumerate(pending_entries):
                    _content = (e.get("content", "") or e.get("summary", "") or "").strip()
                    ok, _ = _generate_entry_summary(e["id"], _content, gemini_key)
                    if ok:
                        success_count += 1
                    else:
                        fail_count += 1
                    prog.progress(
                        (idx + 1) / len(pending_entries),
                        text=f"进度 {idx+1}/{len(pending_entries)}（成功 {success_count}，失败 {fail_count}）",
                    )
                prog.empty()
                st.success(f"批量生成完成：成功 {success_count} 条，失败 {fail_count} 条")
                st.rerun()

        for e in hist:
            status = e.get("agent_summary_status") or "pending"
            if status == "success":
                tag = "有摘要✓"
            elif status == "failed":
                tag = "失败 ✗"
            else:
                tag = _get_text(T, "memory_tab.agent_summary_pending_tag") or "待生成"
            label = f"【{e.get('title', '') or e.get('source_id', '') or e.get('source_type', '')}】 {e.get('created_at', '')} · [{tag}]"
            with st.expander(label, expanded=False):
                content = e.get("content", "") or e.get("summary", "")
                st.caption(_get_text(T, "memory_tab.librarian_summary_label") or (_get_text(T, "memory_tab.agent_summary_label") or "Agent 摘要"))
                if status == "success":
                    st.markdown(e.get("agent_summary", "") or "")
                elif status == "failed":
                    st.caption(_get_text(T, "memory_tab.agent_summary_failed") or "摘要生成失败")
                    if st.button(_get_text(T, "memory_tab.agent_summary_retry_btn") or "重试", key=f"retry_summary_{e.get('id')}"):
                        ok, err = _generate_entry_summary(e["id"], content, defaults.get("gemini_key", ""))
                        if ok:
                            st.rerun()
                        else:
                            st.error(f"摘要生成失败：{err}")
                else:
                    st.caption(_get_text(T, "memory_tab.agent_summary_pending_hint") or "摘要待生成")
                    if st.button(
                        _get_text(T, "memory_tab.agent_summary_generate_btn") or "生成摘要",
                        key=f"gen_summary_{e.get('id')}",
                    ):
                        ok, err = _generate_entry_summary(e["id"], content, defaults.get("gemini_key", ""))
                        if ok:
                            st.rerun()
                        else:
                            st.error(f"摘要生成失败：{err}")
                st.divider()
                st.caption("导入内容")
                st.markdown((content or "")[:2000] + ("..." if len(content or "") > 2000 else ""))
    else:
        st.caption("暂无导入记录，通过下方导入后此处将显示历史与 Agent 摘要。")

    st.divider()
    st.markdown("**导入需求**")
    demand_paste = st.text_area(
        _get_text(T, "memory_tab.demand_paste_label") or "粘贴需求文档内容",
        placeholder=_get_text(T, "memory_tab.demand_paste_placeholder") or "在此粘贴 PRD 或需求文档…",
        height=120,
        key="mem_demand_paste",
        label_visibility="collapsed",
    )
    demand_title_input = st.text_input(
        _get_text(T, "memory_tab.demand_title_label") or "标题（可选）",
        placeholder="如：直播分辨率 AB test 需求",
        key="mem_demand_title",
        label_visibility="collapsed",
    )
    if st.button(_get_text(T, "memory_tab.import_demand_btn") or "导入需求", key="mem_import_demand"):
        if demand_paste and demand_paste.strip():
            rowid, status = add_entry_with_dedup(
                "manual",
                demand_paste.strip(),
                source_id="",
                title=(demand_title_input or "").strip() or "需求文档",
                summary=demand_paste[:500],
                project_id=project_id,
            )
            if status == "skipped":
                st.info(_get_text(T, "memory_tab.history_item_skipped") or "文件未变更，已跳过")
            else:
                try:
                    from context_cache_service import mark_context_cache_dirty

                    mark_context_cache_dirty(f"memory_{status}")
                except ImportError:
                    pass
                with st.spinner(_get_text(T, "memory_tab.agent_summary_pending") or "生成摘要中…"):
                    _generate_entry_summary(rowid, demand_paste.strip(), defaults.get("gemini_key", ""))
                st.success("已导入，可在上方搜索查看")
            st.rerun()
        else:
            st.error(_get_text(T, "memory_tab.import_required") or "请粘贴需求文档内容")

    st.markdown(_get_text(T, "memory_tab.test_cases_section") or "**导入全回归测试用例**")
    st.caption(_get_text(T, "memory_tab.test_cases_caption") or "上传文件或粘贴内容，Agent 将参考既有用例理解项目。")

    _full_regression = get_entry_content(
        TEST_CASES_SOURCE_TYPE,
        "full_regression",
        project_id=project_id,
    )
    if _full_regression:
        _len_chars = len(_full_regression)
        _tpl = _get_text(T, "memory_tab.full_regression_status") or "全回归用例已导入（{count} 字），生成用例时 Agent 将参考理解。"
        st.success("✓ " + _tpl.format(count=_len_chars))

    try:
        from app_ui_components import render_file_uploader

        test_cases_upload_result = render_file_uploader(
            accepted_types=["xlsx", "xls", "csv", "txt"],
            key="test_cases_upload",
            label=_get_text(T, "memory_tab.test_cases_upload_placeholder") or "上传文件",
        )
        test_cases_file = test_cases_upload_result["file"] if test_cases_upload_result else None
    except ImportError:
        test_cases_file = st.file_uploader(
            _get_text(T, "memory_tab.test_cases_upload_placeholder") or "上传文件",
            type=["xlsx", "xls", "csv", "txt"],
            key="test_cases_upload",
            label_visibility="collapsed",
        )

    # 缓存测试用例上传文件，切换 Tab 后仍可使用
    tc_cache_key = "memory_test_cases_upload_cache"
    if test_cases_file:
        try:
            name = getattr(test_cases_file, "name", "") or "测试用例上传"
            data = test_cases_file.read()
            st.session_state[tc_cache_key] = {"name": name, "bytes": data}
        except Exception:
            pass

    test_cases_paste = st.text_area(
        _get_text(T, "memory_tab.test_cases_paste_placeholder") or "或粘贴内容",
        placeholder=_get_text(T, "memory_tab.test_cases_paste_placeholder") or "表格（| 分隔）或纯文本",
        key="test_cases_paste",
        height=100,
        label_visibility="collapsed",
    )
    if st.button(_get_text(T, "memory_tab.test_cases_import_btn") or "导入测试用例", key="mem_import_test_cases"):
        # 始终优先使用缓存中的原始字节，避免二次 read 导致游标在 EOF 位置
        cached_tc = st.session_state.get(tc_cache_key)
        file_for_import = None
        if cached_tc:
            file_for_import = _MemoryUpload(cached_tc["name"], cached_tc["bytes"])
        elif test_cases_file:
            # 理论上上方缓存已存在；此分支仅作为兜底
            try:
                name = getattr(test_cases_file, "name", "") or "测试用例上传"
                data = test_cases_file.read()
                file_for_import = _MemoryUpload(name, data or b"")
            except Exception:
                file_for_import = None

        if file_for_import:
            with st.spinner(_get_text(T, "memory_tab.import_spinner_file") or "解析文件中…"):
                try:
                    content, rows = parse_test_cases_file(file_for_import)
                    if not content.strip():
                        st.warning(_get_text(T, "memory_tab.file_empty") or "文件内容为空")
                    else:
                        rowid, status = add_entry_with_dedup(
                            TEST_CASES_SOURCE_TYPE,
                            content,
                            source_id="full_regression",
                            title="全回归测试用例",
                            summary=content[:500],
                            project_id=project_id,
                        )
                        if status == "skipped":
                            st.info(_get_text(T, "memory_tab.history_item_skipped") or "文件未变更，已跳过")
                        else:
                            try:
                                from context_cache_service import mark_context_cache_dirty

                                mark_context_cache_dirty(f"test_cases_{status}")
                            except ImportError:
                                pass
                            with st.spinner(_get_text(T, "memory_tab.agent_summary_pending") or "生成摘要中…"):
                                _generate_entry_summary(rowid, content, defaults.get("gemini_key", ""))
                            st.success(f"已导入 {rows} 行（{len(content)} 字），Agent 将参考既有用例。")
                        st.rerun()
                except Exception as ex:
                    st.error(f"{_get_text(T, 'memory_tab.parse_fail') or '解析失败'}: {ex}")
        elif test_cases_paste and test_cases_paste.strip():
            content = test_cases_paste.strip()
            rowid, status = add_entry_with_dedup(
                TEST_CASES_SOURCE_TYPE,
                content,
                source_id="full_regression",
                title="全回归测试用例",
                summary=content[:500],
                project_id=project_id,
            )
            if status == "skipped":
                st.info(_get_text(T, "memory_tab.history_item_skipped") or "文件未变更，已跳过")
            else:
                try:
                    from context_cache_service import mark_context_cache_dirty

                    mark_context_cache_dirty(f"test_cases_{status}")
                except ImportError:
                    pass
                with st.spinner(_get_text(T, "memory_tab.agent_summary_pending") or "生成摘要中…"):
                    _generate_entry_summary(rowid, content, defaults.get("gemini_key", ""))
                st.success(f"已导入（{len(content)} 字），Agent 将参考既有用例。")
            st.rerun()
        else:
            st.error(_get_text(T, "memory_tab.import_required") or "请上传文件或粘贴内容")

    # === 导入设计图 ===
    st.markdown("**" + (_get_text(T, "memory_tab.design_mockup_section") or "导入设计图") + "**")
    st.caption(
        _get_text(T, "memory_tab.design_mockup_caption")
        or "上传 Figma/截图等设计稿，Agent 将理解界面布局与交互细节。"
    )

    try:
        from app_ui_components import render_file_uploader

        design_upload_result = render_file_uploader(
            accepted_types=["png", "jpg", "jpeg", "pdf"],
            key="design_mockup_upload",
            label=_get_text(T, "memory_tab.design_mockup_upload_label")
            or "上传设计图（PNG / JPG / PDF，最多 5 个，单文件 ≤10MB）",
        )
        design_files = design_upload_result.get("files") if design_upload_result else None
    except ImportError:
        design_files = st.file_uploader(
            _get_text(T, "memory_tab.design_mockup_upload_label")
            or "上传设计图（PNG / JPG / PDF，最多 5 个，单文件 ≤10MB）",
            type=["png", "jpg", "jpeg", "pdf"],
            key="design_mockup_upload",
            accept_multiple_files=True,
            label_visibility="collapsed",
        )

    # 缓存设计图上传文件，切换 Tab 后仍可使用
    design_cache_key = "memory_design_mockup_upload_cache"
    if design_files:
        cached_list: list[dict[str, bytes]] = []
        for f in (design_files or []):
            try:
                name = getattr(f, "name", "") or "设计图"
                data = f.read()
            except Exception:
                continue
            cached_list.append({"name": name, "bytes": data})
        if cached_list:
            st.session_state[design_cache_key] = cached_list

    if st.button(
        _get_text(T, "memory_tab.design_mockup_import_btn") or "解析并导入",
        key="mem_import_design",
    ):
        gemini_key = defaults.get("gemini_key", "")
        if not gemini_key:
            st.error(_get_text(T, "memory_tab.design_mockup_key_missing") or "请先在设置中配置 Gemini API Key")
        else:
            cached_design = st.session_state.get(design_cache_key) or []
            files_source = list(design_files) if design_files else [
                _MemoryUpload(item["name"], item["bytes"]) for item in cached_design
            ]
            if not files_source:
                st.error(_get_text(T, "memory_tab.import_required") or "请上传设计图文件")
                return

            files_to_process = files_source[:5]
            if len(files_to_process) < len(files_source):
                st.warning("已超过 5 个文件限制，只处理前 5 个。")

            import_count = 0
            skip_count = 0
            fail_msgs: list[str] = []

            import hashlib
            import time

            for uploaded_file in files_to_process:
                file_name = getattr(uploaded_file, "name", "设计图")
                raw_bytes = uploaded_file.read()

                # 文件大小检查（10MB）
                if len(raw_bytes) > 10 * 1024 * 1024:
                    fail_msgs.append(f"{file_name}：文件超过 10MB，已跳过")
                    continue

                file_hash = hashlib.sha256(raw_bytes).hexdigest()

                ext = file_name.lower().rsplit(".", 1)[-1] if "." in file_name else "png"
                mime_map = {
                    "jpg": "image/jpeg",
                    "jpeg": "image/jpeg",
                    "png": "image/png",
                    "pdf": "application/pdf",
                }
                mime_type = mime_map.get(ext, "image/png")

                pages_bytes: list[tuple[bytes, str]] = []
                if ext == "pdf":
                    try:
                        from io import BytesIO

                        import fitz  # type: ignore[import]  # PyMuPDF 可选

                        doc = fitz.open(stream=raw_bytes, filetype="pdf")
                        total_pages = len(doc)
                        max_pages = 5
                        if total_pages > max_pages:
                            st.info(
                                (_get_text(T, "memory_tab.design_mockup_pdf_truncated") or "PDF 超过 5 页，已只处理前 5 页")
                                + f"（共 {total_pages} 页）"
                            )
                        for i in range(min(total_pages, max_pages)):
                            page = doc[i]
                            pix = page.get_pixmap(dpi=150)
                            img_bytes = pix.tobytes("png")
                            pages_bytes.append((img_bytes, "image/png"))
                        doc.close()
                    except ImportError:
                        # 无 PyMuPDF 时，直接把 PDF 字节发给 Gemini
                        pages_bytes = [(raw_bytes, "application/pdf")]
                    except Exception as ex:
                        fail_msgs.append(f"{file_name}：PDF 解析失败 - {ex}")
                        continue
                else:
                    pages_bytes = [(raw_bytes, mime_type)]

                page_descriptions: list[str] = []
                for idx, (pb, pm) in enumerate(pages_bytes):
                    with st.spinner(
                        (_get_text(T, "memory_tab.design_mockup_parsing") or "正在用 Gemini Vision 解析设计图…")
                        + (f"（第 {idx + 1} 页）" if len(pages_bytes) > 1 else "")
                    ):
                        desc, err = _parse_design_image_with_gemini(pb, pm, gemini_key)
                    if err:
                        fail_msgs.append(f"{file_name} 第{idx+1}页：{err}")
                    else:
                        if len(pages_bytes) > 1:
                            page_descriptions.append(f"### 第 {idx+1} 页\n\n{desc}")
                        else:
                            page_descriptions.append(desc)
                    if idx < len(pages_bytes) - 1:
                        time.sleep(1)

                if not page_descriptions:
                    continue

                full_description = f"# 设计图：{file_name}\n\n" + "\n\n---\n\n".join(page_descriptions)
                rowid, status = add_entry_with_dedup(
                    DESIGN_MOCKUP_SOURCE_TYPE,
                    full_description,
                    source_id=file_hash[:16],
                    title=f"设计图：{file_name}",
                    summary=full_description[:500],
                    project_id=project_id,
                )

                if status == "skipped":
                    skip_count += 1
                else:
                    import_count += 1
                    try:
                        from context_cache_service import mark_context_cache_dirty

                        mark_context_cache_dirty(f"design_{status}")
                    except ImportError:
                        pass
                    with st.spinner(_get_text(T, "memory_tab.agent_summary_pending") or "生成摘要中…"):
                        _generate_entry_summary(rowid, full_description, gemini_key)

            if import_count:
                st.success(
                    (_get_text(T, "memory_tab.design_mockup_success") or "设计图已解析导入，下次生成用例时 Agent 将参考。")
                    + f"（{import_count} 个）"
                )
            if skip_count:
                st.info(
                    (_get_text(T, "memory_tab.design_mockup_skipped") or "设计图未变更，已跳过")
                    + f"（{skip_count} 个）"
                )
            for msg in fail_msgs:
                st.error(msg)
            if import_count or skip_count:
                st.rerun()

    st.divider()
    with st.expander(_get_text(T, "memory_tab.memory_summary_section") or "项目记忆摘要（高级）", expanded=False):
        mem = load_project_memory(project_id=project_id)
        new_mem = st.text_area(
            _get_text(T, "memory_tab.memory_text_label") or "内容",
            value=mem, height=180, key="project_memory_text",
        )
        c1, c2 = st.columns(2)
        with c1:
            if st.button(_get_text(T, "memory_tab.save_summary_btn") or "保存摘要", key="mem_save_summary"):
                os.makedirs(CONFIG_DIR, exist_ok=True)
                if project_id == PROJECT_RM11:
                    path = os.path.join(CONFIG_DIR, "project_memory_rm11.md")
                else:
                    path = PROJECT_MEMORY_PATH
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new_mem)
                try:
                    from context_cache_service import mark_context_cache_dirty

                    mark_context_cache_dirty("project_memory_saved")
                except ImportError:
                    pass
                st.success("已保存")
        with c2:
            if st.button(_get_text(T, "memory_tab.append_from_run_btn") or "从本次运行追加", key="mem_update_from_run"):
                if st.session_state.get("app_last_run") and st.session_state.get("app_last_demand_snippet"):
                    snippet = st.session_state["app_last_demand_snippet"]
                    result = st.session_state["app_last_run"].get("result_str", "")[:2000]
                    addition = f"【最近一次需求摘要】\n{snippet}\n\n【产出摘要】\n{result}"
                    update_project_memory(addition)
                    add_entry(
                        "run_summary",
                        result,
                        source_id="",
                        title="最近一次运行",
                        summary=snippet,
                        project_id=project_id,
                    )
                    st.success("已追加到项目记忆")
                else:
                    st.info(_get_text(T, "memory_tab.run_first_hint") or "请先在「生成用例」页运行一次")

def _render_module_chat(T: dict, defaults: dict):
    """工作台模块：文档问答。"""
    st.subheader(_get_text(T, "chat_tab.section_title") or "与产品文档管理 Agent 沟通")
    st.caption(_get_text(T, "chat_tab.section_desc") or "Agent 可理解全部需求文档。选择项目整体记忆或手动粘贴文档内容。")

    chat_options = ["memory", "paste"] if MEMORY_AVAILABLE else ["paste"]
    doc_source = st.radio(
        _get_text(T, "chat_tab.doc_source_label") or "文档来源",
        options=chat_options,
        format_func=lambda x: {
            "memory": _get_text(T, "chat_tab.doc_source_memory") or "项目整体记忆（全部需求文档）",
            "paste": _get_text(T, "chat_tab.doc_source_paste") or "手动粘贴文档内容",
        }[x],
        key="chat_doc_source",
    )
    doc_context = ""
    if doc_source == "memory":
        if not MEMORY_AVAILABLE:
            st.info(_get_text(T, "chat_tab.doc_source_empty") or "当前运行环境未启用项目记忆存储，请使用「粘贴文档内容」模式。")
        else:
            try:
                from memory_store import get_all_demands_full_for_chat
            except ImportError:
                st.info(_get_text(T, "chat_tab.doc_source_empty") or "当前运行环境未启用项目记忆存储，请使用「粘贴文档内容」模式。")
                doc_context = ""
            else:
                doc_context = get_all_demands_full_for_chat(
                    limit=30,
                    project_id=_get_current_project(),
                ).strip()
                if not doc_context:
                    st.info(_get_text(T, "chat_tab.doc_source_empty") or "项目记忆暂无需求文档。请先在「项目记忆」页导入。")
    else:
        doc_context = st.text_area(
            _get_text(T, "chat_tab.paste_placeholder") or "在此粘贴需求文档内容",
            height=150,
            key="chat_paste_doc",
        ).strip()

    if "app_doc_chat_messages" not in st.session_state:
        st.session_state["app_doc_chat_messages"] = []

    if st.session_state["app_doc_chat_messages"] and st.button(_get_text(T, "chat_tab.clear_btn") or "清空对话", key="chat_clear"):
        st.session_state["app_doc_chat_messages"] = []
        st.rerun()

    _doc_title = "无文档"
    if doc_context:
        _doc_title = (_get_text(T, "chat_tab.doc_source_memory") or "项目整体记忆") if doc_source == "memory" else (_get_text(T, "chat_tab.doc_source_paste") or "当前文档")
    st.caption(f"**当前文档：** {_doc_title}")

    for msg in st.session_state["app_doc_chat_messages"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if doc_context:
        if st.button(_get_text(T, "chat_tab.quick_summary") or "请总结这份文档的核心要点与潜在风险", key="quick_summary_btn"):
            user_q = _get_text(T, "chat_tab.quick_summary") or "请总结这份文档的核心要点与潜在风险"
            st.session_state["app_doc_chat_messages"].append({"role": "user", "content": user_q})
            with st.chat_message("assistant"):
                with st.spinner(_get_text(T, "chat_tab.thinking") or "文档 Agent 正在思考…"):
                    try:
                        os.environ["GEMINI_API_KEY"] = defaults.get("gemini_key") or os.environ.get("GEMINI_API_KEY", "")
                        os.environ["GEMINI_MODEL"] = defaults.get("gemini_model") or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
                        reply = chat_with_document_agent(
                            user_message=user_q,
                            document_context=doc_context,
                            project_context=get_project_context_for_agent(include_store=False),
                        )
                    except Exception as e:
                        reply = f"调用失败: {e}"
                st.markdown(reply)
            st.session_state["app_doc_chat_messages"].append({"role": "assistant", "content": reply})
            st.rerun()

    user_input = st.chat_input(_get_text(T, "chat_tab.chat_placeholder") or "输入问题…")
    if user_input and doc_context:
        st.session_state["app_doc_chat_messages"].append({"role": "user", "content": user_input})
        with st.chat_message("assistant"):
            with st.spinner(_get_text(T, "chat_tab.thinking") or "文档 Agent 正在思考…"):
                try:
                    os.environ["GEMINI_API_KEY"] = defaults.get("gemini_key") or os.environ.get("GEMINI_API_KEY", "")
                    os.environ["GEMINI_MODEL"] = defaults.get("gemini_model") or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
                    reply = chat_with_document_agent(
                        user_message=user_input,
                        document_context=doc_context,
                        project_context=get_project_context_for_agent(include_store=False),
                    )
                except Exception as e:
                    reply = f"调用失败: {e}"
            st.markdown(reply)
        st.session_state["app_doc_chat_messages"].append({"role": "assistant", "content": reply})
        st.rerun()
    elif user_input and not doc_context:
        st.warning(_get_text(T, "chat_tab.doc_source_empty") or "请先选择并加载文档内容。")


def _render_module_settings(T: dict, defaults: dict):
    """工作台模块：设置（模型、凭证）。"""
    if st.button(_get_text(T, "app.back_btn") or "← 返回", key="settings_back"):
        st.session_state["current_page"] = MODULE_RUN
        st.rerun()
    st.caption("配置 API 凭证与模型，保存后生成用例时将自动使用。")
    with st.container():
        gemini_key = st.text_input(
            _get_text(T, "run_tab.gemini_key_label") or "Gemini API Key",
            value=defaults.get("gemini_key", ""), type="password",
            help=_get_text(T, "run_tab.gemini_key_help") or "用于驱动四个 Agent 生成用例",
            key="settings_gemini_key",
        )
        gemini_models_list, default_model = _load_models()
        _model_opts = [m[0] for m in gemini_models_list]
        _model_idx = next((i for i, (k, _) in enumerate(gemini_models_list) if k == (defaults.get("gemini_model") or default_model)), 0)
        _model_col, _quota_col = st.columns([3, 1])
        with _model_col:
            gemini_model = st.selectbox(
                _get_text(T, "run_tab.gemini_model_label") or "Gemini 模型",
                options=_model_opts,
                index=_model_idx,
                format_func=lambda x: dict(gemini_models_list).get(x, x),
                help=_get_text(T, "run_tab.gemini_model_help") or "免费推荐：2.5 Flash-Lite；高质量：2.5 Flash。",
                key="settings_gemini_model",
            )
        with _quota_col:
            _quota_url = _get_text(T, "run_tab.gemini_quota_url") or "https://aistudio.google.com/rate-limit"
            st.link_button(
                _get_text(T, "run_tab.gemini_quota_btn") or "查看剩余额度",
                _quota_url,
                help=_get_text(T, "run_tab.gemini_quota_help") or "在 Google AI Studio 查看用量与限额（新开页）",
            )
        if st.button(_get_text(T, "run_tab.save_defaults_btn") or "保存到本地（下次无需再填）", type="primary", key="settings_save_defaults", help=_get_text(T, "run_tab.save_defaults_help") or "仅限本机；共享电脑建议用环境变量"):
            mode = _save_defaults(gemini_key or "", gemini_model)
            st.success((_get_text(T, "run_tab.save_success") or "已保存到本地") + f"（{mode}）")
            st.rerun()

    ver_info = _load_version()
    ver_str = str(ver_info.get("version", "") or "").strip()
    if ver_str:
        ver_label = _get_text(T, "app.version_label") or "版本"
        ver_display = f"{ver_label}: {ver_str}"
        build_str = str(ver_info.get("build_time", "") or "").strip()
        if build_str:
            ver_display += f" ({build_str})"
        st.divider()
        st.caption(ver_display)


def main():
    """入口：直接进入主应用。"""
    T = _load_ui_texts()
    page_title = _get_text(T, "app.page_title") or "用例工坊 · AI 测试协作平台"
    st.set_page_config(page_title=page_title, layout="wide", initial_sidebar_state="expanded")
    _render_main_app(T)


if __name__ == "__main__":
    main()
