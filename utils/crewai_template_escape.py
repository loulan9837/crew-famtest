# -*- coding: utf-8 -*-
"""
CrewAI kickoff 会把 Task description 中的 {name} 当作模板变量注入。
凡经 inputs 或 description 拼接进入该模板的用户/Agent 文本，须先转义字面花括号。

注意：调用方只应在一处转义；若在 pipeline 与 crew_test 各转义一次会导致双次转义。
当前约定：由 crew_test._run_crew_sequential（及 _build_crew_from_config 的 project_context）统一处理。
"""


def escape_curly_braces_for_crewai_inputs(text: str) -> str:
    """将 { / } 转为 {{ / }}，模板填充后模型仍看到单花括号（如 JSON 中的字段名）。"""
    if not text:
        return ""
    return str(text).replace("{", "{{").replace("}", "}}")
