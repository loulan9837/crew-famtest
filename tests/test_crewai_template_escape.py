# -*- coding: utf-8 -*-
from utils.crewai_template_escape import escape_curly_braces_for_crewai_inputs
from crew_test import _sanitize_task_template
import re


def test_escape_curly_passcode_example():
    s = '字段 "passcode": {passcode}'
    out = escape_curly_braces_for_crewai_inputs(s)
    assert out == '字段 "passcode": {{passcode}}'


def test_escape_empty():
    assert escape_curly_braces_for_crewai_inputs("") == ""


def test_sanitize_task_template_keeps_known_placeholders():
    s = "需求:{prd_content} 示例字段:{passcode}"
    out = _sanitize_task_template(s, {"prd_content", "task1_output", "task2_output", "task3_output", "project_context"})
    assert "{prd_content}" in out
    assert "{{passcode}}" in out
    assert re.search(r"(?<!\{)\{passcode\}(?!\})", out) is None
