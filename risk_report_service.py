# -*- coding: utf-8 -*-
"""
需求风险分析服务：独立调用分析 Agent 对文档做风险评估，不参与四 Agent 协作。
"""
from __future__ import annotations

import os


def generate_risk_assessment_report(
    document_content: str,
    gemini_model: str = "",
    gemini_key: str = "",
) -> str:
    """
    独立调用分析 Agent 对文档做需求风险分析，返回 Markdown 表格。
    不参与四 Agent 流水线，仅使用 LLM 单次调用。
    """
    if not (document_content or "").strip():
        raise ValueError("文档内容为空")

    from crew_test import _resolve_gemini_model, desensitize_for_llm
    from langchain_google_genai import ChatGoogleGenerativeAI

    key = (gemini_key or "").strip() or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ValueError("请先配置 GEMINI_API_KEY")

    model = _resolve_gemini_model(
        (gemini_model or "").strip() or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
    )
    llm = ChatGoogleGenerativeAI(model=model, google_api_key=key, temperature=0.3)

    # 入参脱敏：传给大模型前对文档内容做掩码处理
    safe_doc = desensitize_for_llm(document_content.strip())[:30000]

    prompt = """你是一位资深需求分析师。请对以下需求文档进行深度扫描，产出《需求风险评估报告》。

必须按以下三个维度分析，并以 **Markdown 表格** 输出，表头固定为：
| 维度 | 模块 | 风险类型 | 风险概述 | 优先级建议 | 问题描述 | 建议/疑问 |

维度取值：模糊性审查、逻辑冲突与遗漏、极端场景推演。

要求：
1. **模糊性审查**：找出描述不清的词汇、缺失的 UI 交互细节。
2. **逻辑冲突与遗漏**：新老逻辑互斥、状态闭环、权限边界。
3. **极端场景推演**：数值边界、并发场景。

若某维度无明显问题，可写「无」；每条风险占一行。直接输出表格，不要多余说明。

【需求文档】
{document}
""".replace("{document}", safe_doc)

    msg = llm.invoke(prompt)
    return (msg.content or "").strip()


def generate_requirement_review_questions(
    document_content: str,
    gemini_model: str = "",
    gemini_key: str = "",
) -> str:
    """
    需求评审：单次 LLM 调用，返回 Markdown 形式的「核心疑问点」。
    与 generate_risk_assessment_report 独立，不共用入口。
    """
    if not (document_content or "").strip():
        raise ValueError("文档内容为空")

    import time

    from crew_test import _resolve_gemini_model, desensitize_for_llm
    from langchain_google_genai import ChatGoogleGenerativeAI

    key = (gemini_key or "").strip() or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ValueError("请先配置 GEMINI_API_KEY")

    model = _resolve_gemini_model(
        (gemini_model or "").strip() or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
    )
    safe_doc = desensitize_for_llm(document_content.strip())[:30000]

    prompt = f"""你是一位资深测试工程师。请审阅以下需求，指出其中逻辑模糊、不完整或存在风险的地方，直接列出几条核心疑问点，不要废话。

【需求文档】
{safe_doc}"""

    llm = ChatGoogleGenerativeAI(model=model, google_api_key=key, temperature=0.3)

    for attempt in range(3):
        try:
            msg = llm.invoke(prompt)
            out = (msg.content or "").strip()
            if not out:
                raise ValueError("模型返回为空")
            return out
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "503" in err_str or "timeout" in err_str.lower():
                if attempt < 2:
                    time.sleep(5)
                    continue
            raise
