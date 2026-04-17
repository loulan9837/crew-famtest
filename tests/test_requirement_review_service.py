# -*- coding: utf-8 -*-
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk_report_service import generate_requirement_review_questions  # noqa: E402


def test_generate_requirement_review_questions_empty_raises():
    with pytest.raises(ValueError, match="为空"):
        generate_requirement_review_questions("   ")


def test_generate_requirement_review_questions_no_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GEMINI"):
        generate_requirement_review_questions("hello world", gemini_key="")
