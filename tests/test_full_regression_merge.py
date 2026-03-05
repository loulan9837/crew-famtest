# -*- coding: utf-8 -*-
"""全回归用例行级合并逻辑单元测试"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app_ui import _normalize_full_regression_lines, _merge_full_regression  # type: ignore[attr-defined]


def test_normalize_full_regression_lines():
    text = "\n  A | 1  \n\nB | 2\n   \nC | 3  "
    lines = _normalize_full_regression_lines(text)
    assert lines == ["A | 1", "B | 2", "C | 3"]


def test_merge_full_regression_basic_union_and_order():
    existing = "A | 1\nB | 2\nC | 3"
    new = "B | 2\nC | 3\nD | 4"

    merged, added = _merge_full_regression(existing, new)
    merged_lines = merged.splitlines()

    # 旧内容在前，新内容追加在后，重复行只保留一份
    assert merged_lines == ["A | 1", "B | 2", "C | 3", "D | 4"]
    assert added == 1  # 本次仅新增 D | 4 一行


def test_merge_full_regression_all_new_when_existing_empty():
    existing = ""
    new = "X | 9\nY | 10"

    merged, added = _merge_full_regression(existing, new)
    merged_lines = merged.splitlines()

    assert merged_lines == ["X | 9", "Y | 10"]
    assert added == 2


def test_merge_full_regression_no_change_when_identical():
    existing = "A | 1\nB | 2"
    new = "A | 1\nB | 2"

    merged, added = _merge_full_regression(existing, new)
    merged_lines = merged.splitlines()

    assert merged_lines == ["A | 1", "B | 2"]
    assert added == 0

