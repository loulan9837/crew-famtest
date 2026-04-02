# -*- coding: utf-8 -*-
"""
业务逻辑层：Quip → 四 Agent → 测试用例流水线
与 Streamlit UI 解耦，供 app_ui 调用。
支持：Quip 拉取、文件上传（.md + .xlsx）两种输入方式。
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, TypedDict

from crew_test import (
    load_demand_from_quip,
    run_pipeline,
    _parse_markdown_tables,
    tables_to_text,
    export_tables_to_excel_bytes,
)
from memory_store import (
    TEST_CASES_SOURCE_TYPE,
    add_entry,
    get_entry_content,
)


class QuipToCasesResult(TypedDict):
    ok: bool
    error: str | None
    last_run: dict[str, Any] | None
    demand_snippet: str
    demand_full: str
    archive_suffix: str
    archive_warning: str


class UploadToCasesResult(TypedDict):
    ok: bool
    error: str | None
    understanding: str
    issues: str
    cases_md: str
    excel_bytes: bytes | None


def run_quip_to_cases(
    quip_url: str,
    quip_token: str,
    gemini_key: str,
    gemini_model: str,
    export_quip: bool,
    export_sheets: bool,
    export_quip_target: str | None,
    auto_archive: bool,
    output_dir: str,
) -> QuipToCasesResult:
    """封装 Quip 拉取 + 四 Agent 流水线 + 可选归档的业务流程，不依赖 Streamlit。"""
    # 环境变量注入：保持与命令行脚本一致的行为；兼容已弃用模型名
    from crew_test import _resolve_gemini_model
    os.environ["QUIP_ACCESS_TOKEN"] = quip_token or os.environ.get("QUIP_ACCESS_TOKEN", "")
    os.environ["GEMINI_API_KEY"] = gemini_key or os.environ.get("GEMINI_API_KEY", "")
    os.environ["GEMINI_MODEL"] = _resolve_gemini_model(
        gemini_model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
    )

    if not os.environ.get("GEMINI_API_KEY"):
        return {
            "ok": False,
            "error": "未配置 Gemini API Key",
            "last_run": None,
            "demand_snippet": "",
            "demand_full": "",
            "archive_suffix": "",
            "archive_warning": "",
        }

    quip = (quip_url or "").strip()
    if not quip:
        return {
            "ok": False,
            "error": "需求文档链接不能为空",
            "last_run": None,
            "demand_snippet": "",
            "demand_full": "",
            "archive_suffix": "",
            "archive_warning": "",
        }

    try:
        demand, demand_title = load_demand_from_quip(quip, return_title=True)
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"拉取文档失败: {e}",
            "last_run": None,
            "demand_snippet": "",
            "demand_full": "",
            "archive_suffix": "",
            "archive_warning": "",
        }

    if not demand or not demand.strip():
        return {
            "ok": False,
            "error": "文档内容为空",
            "last_run": None,
            "demand_snippet": "",
            "demand_full": "",
            "archive_suffix": "",
            "archive_warning": "",
        }

    try:
        out = run_pipeline(
            demand,
            output_dir=output_dir,
            export_excel=True,
            export_quip=export_quip or bool(export_quip_target and export_quip_target.strip()),
            export_sheets=export_sheets,
            export_quip_target=(export_quip_target or "").strip() or None,
            demand_title=demand_title,
            return_details=True,
        )
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"执行失败: {e}",
            "last_run": None,
            "demand_snippet": "",
            "demand_full": demand,
            "archive_suffix": "",
            "archive_warning": "",
        }

    if isinstance(out, dict):
        last_run: dict[str, Any] = dict(out)
        last_run["demand_title"] = demand_title or ""
    else:
        last_run = {
            "result_str": str(out),
            "step_outputs": [],
            "excel_path": None,
            "quip_url": None,
            "sheets_url": None,
            "timestamp": "",
            "txt_path": "",
            "demand_title": demand_title or "",
        }

    archive_suffix = ""
    archive_warning = ""
    if auto_archive:
        result_str = str(last_run.get("result_str", ""))
        tables = _parse_markdown_tables(result_str)
        if tables:
            new_text = tables_to_text(tables)
            section = f"\n\n【{datetime.now().strftime('%Y-%m-%d %H:%M')} 新增】\n{new_text}"
            current = get_entry_content(TEST_CASES_SOURCE_TYPE, "full_regression") or ""
            updated = (current + section).strip()
            add_entry(
                TEST_CASES_SOURCE_TYPE,
                updated,
                source_id="full_regression",
                title="全回归测试用例",
                summary=updated[:500],
            )
            archive_suffix = "，已归档到全回归用例"
        else:
            archive_warning = "归档失败：未解析到有效表格。"

    snippet = demand[:500] + ("..." if len(demand) > 500 else "")
    return {
        "ok": True,
        "error": None,
        "last_run": last_run,
        "demand_snippet": snippet,
        "demand_full": demand,
        "archive_suffix": archive_suffix,
        "archive_warning": archive_warning,
    }


def run_upload_to_cases(
    demand_md: str,
    existing_cases: str,
    gemini_key: str,
    gemini_model: str,
    project_context: str = "",
) -> UploadToCasesResult:
    """文件上传模式：基于 .md 需求 + .xlsx 既有用例，跑四 Agent 流水线。
    返回理解内容（task2）、问题点（task1）、新用例表（task3）、Excel 二进制。
    仅支持 Excel 下载，不落盘、不导出 Quip/Sheets。"""
    from crew_test import get_project_context_for_agent

    os.environ["GEMINI_API_KEY"] = gemini_key or os.environ.get("GEMINI_API_KEY", "")
    os.environ["GEMINI_MODEL"] = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
    from crew_test import _resolve_gemini_model
    os.environ["GEMINI_MODEL"] = _resolve_gemini_model(gemini_model or os.environ.get("GEMINI_MODEL", ""))

    if not os.environ.get("GEMINI_API_KEY"):
        return {
            "ok": False,
            "error": "未配置 Gemini API Key",
            "understanding": "",
            "issues": "",
            "cases_md": "",
            "excel_bytes": None,
        }

    demand = (demand_md or "").strip()
    if not demand:
        return {
            "ok": False,
            "error": "需求文档不能为空，请至少上传 1 个 .md 文件",
            "understanding": "",
            "issues": "",
            "cases_md": "",
            "excel_bytes": None,
        }

    base_ctx = (project_context or "").strip() or get_project_context_for_agent()
    if (existing_cases or "").strip():
        base_ctx = (base_ctx + "\n\n【既有测试用例】\n\n" + existing_cases.strip()).strip()

    try:
        out = run_pipeline(
            demand,
            output_dir="output",
            export_excel=False,
            export_quip=False,
            export_sheets=False,
            project_context=base_ctx,
            return_details=True,
        )
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "understanding": "",
            "issues": "",
            "cases_md": "",
            "excel_bytes": None,
        }

    if not isinstance(out, dict):
        return {
            "ok": False,
            "error": "流水线返回格式异常",
            "understanding": "",
            "issues": "",
            "cases_md": "",
            "excel_bytes": None,
        }

    step_outputs = out.get("step_outputs") or []
    understanding = ""
    issues = ""
    cases_md = ""

    for s in step_outputs:
        tid = s.get("task") or ""
        content = (s.get("content") or "").strip()
        if tid == "task1":
            issues = content
        elif tid == "task2":
            understanding = content
        elif tid == "task3":
            cases_md = content

    # 优先从 task3（用例表）解析表格；若无则从 result_str（通常为 task4 输出，可能含修正后表格）尝试
    result_str = (out.get("result_str") or "").strip()
    tables = _parse_markdown_tables(cases_md) if (cases_md or "").strip() else []
    if not tables and result_str:
        tables = _parse_markdown_tables(result_str)
    excel_bytes = export_tables_to_excel_bytes(tables) if tables else None

    return {
        "ok": True,
        "error": None,
        "understanding": understanding,
        "issues": issues,
        "cases_md": cases_md,
        "excel_bytes": excel_bytes,
    }
