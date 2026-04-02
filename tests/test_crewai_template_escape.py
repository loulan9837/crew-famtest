# -*- coding: utf-8 -*-
from utils.crewai_template_escape import escape_curly_braces_for_crewai_inputs


def test_escape_curly_passcode_example():
    s = '字段 "passcode": {passcode}'
    out = escape_curly_braces_for_crewai_inputs(s)
    assert out == '字段 "passcode": {{passcode}}'


def test_escape_empty():
    assert escape_curly_braces_for_crewai_inputs("") == ""
