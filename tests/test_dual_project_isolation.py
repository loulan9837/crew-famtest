# -*- coding: utf-8 -*-
"""
双项目工作台 · 数据隔离集成测试

模拟流程：Fambase 下导入/生成 → 切到 RM11 导入/生成 → 切回 Fambase 验证数据完全不串。
使用临时 DB 与临时历史 JSON，不污染生产数据。
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 临时目录：run_history 要求 HISTORY_JSON 在 OUTPUT_DIR 内，故用同一临时目录
TEST_OUTPUT_DIR = tempfile.mkdtemp(prefix="test_dual_project_")
TEST_MEMORY_DB = os.path.join(TEST_OUTPUT_DIR, "memory.db")
TEST_HISTORY_JSON = os.path.join(TEST_OUTPUT_DIR, "generate_history.json")


def setup_module():
    """测试前：确保临时目录存在并清理可能存在的旧 DB。"""
    os.makedirs(TEST_OUTPUT_DIR, exist_ok=True)
    if os.path.isfile(TEST_MEMORY_DB):
        os.remove(TEST_MEMORY_DB)


def teardown_module():
    """测试后：删除临时目录。"""
    import shutil
    if os.path.isdir(TEST_OUTPUT_DIR):
        try:
            shutil.rmtree(TEST_OUTPUT_DIR, ignore_errors=True)
        except OSError:
            pass


@pytest.fixture(scope="module")
def patch_storage():
    """在模块级测试中统一使用临时 DB 与临时历史 JSON（同目录以满足 run_history 路径校验）。"""
    import memory_store as ms  # noqa: E402
    import run_history as rh  # noqa: E402

    orig_db = ms.MEMORY_DB_PATH
    orig_output = rh.OUTPUT_DIR
    orig_json = rh.HISTORY_JSON
    ms.MEMORY_DB_PATH = TEST_MEMORY_DB
    rh.OUTPUT_DIR = TEST_OUTPUT_DIR
    rh.HISTORY_JSON = TEST_HISTORY_JSON
    os.makedirs(TEST_OUTPUT_DIR, exist_ok=True)

    yield ms, rh

    ms.MEMORY_DB_PATH = orig_db
    rh.OUTPUT_DIR = orig_output
    rh.HISTORY_JSON = orig_json
    # 关闭后端连接，避免后续 test_memory_store 复用已删除路径导致 disk I/O error
    if getattr(ms, "_backend", None) is not None and hasattr(ms._backend, "_conn"):
        if ms._backend._conn is not None:
            try:
                ms._backend._conn.close()
            except Exception:
                pass
        ms._backend._conn = None
        if hasattr(ms._backend, "_tables_ready"):
            ms._backend._tables_ready = False


def test_dual_project_isolation_flow(patch_storage):
    """
    交互检查用例（自动化版）：
    1. Fambase 下导入一条需求 + 生成一条历史
    2. 切到 RM11：导入一条需求 + 生成一条历史
    3. 切回 Fambase：验证只能看到 Fambase 的导入与历史，看不到 RM11 数据
    4. 再以 RM11 视角：验证只能看到 RM11 的导入与历史，看不到 Fambase 数据
    """
    ms, rh = patch_storage

    # ---------- 1. Fambase：导入需求 + 生成历史 ----------
    rid_f1, _ = ms.add_entry_with_dedup(
        "manual",
        "Fambase 专属需求：直播分辨率 AB test",
        title="Fambase-PRD-1",
        summary="Fambase 需求摘要",
        project_id="FAMBASE",
    )
    assert rid_f1 > 0

    rh.add_run_record(
        source_type="粘贴",
        demand_title="Fambase 需求标题",
        result_str="Fambase 生成结果预览",
        project_id="FAMBASE",
    )

    # ---------- 2. RM11：导入需求 + 生成历史 ----------
    rid_r1, _ = ms.add_entry_with_dedup(
        "manual",
        "RM11 专属需求：风控规则引擎配置",
        title="RM11-PRD-1",
        summary="RM11 需求摘要",
        project_id="RM11",
    )
    assert rid_r1 > 0

    rh.add_run_record(
        source_type="上传",
        demand_title="RM11 需求标题",
        result_str="RM11 生成结果预览",
        project_id="RM11",
    )

    # ---------- 3. 切回 Fambase 视角：仅能看到 Fambase 数据 ----------
    fam_recent = ms.list_recent(limit=20, project_id="FAMBASE")
    fam_ids = {e.get("id") for e in fam_recent}
    fam_titles = [e.get("title") or "" for e in fam_recent]

    assert rid_f1 in fam_ids, "Fambase 视角应能看到 Fambase 导入的条目"
    assert rid_r1 not in fam_ids, "Fambase 视角不应看到 RM11 导入的条目"
    assert any("Fambase" in t for t in fam_titles), "应有 Fambase 相关标题"
    assert not any("RM11" in t for t in fam_titles), "Fambase 列表里不应出现 RM11 标题"

    fam_history = rh.list_run_records(limit=20, project_id="FAMBASE")
    fam_demands = [r.get("demand_title") or "" for r in fam_history]
    assert any("Fambase" in d for d in fam_demands), "Fambase 历史中应有 Fambase 生成记录"
    assert not any("RM11" in d for d in fam_demands), "Fambase 历史中不应出现 RM11 生成记录"

    # ---------- 4. RM11 视角：仅能看到 RM11 数据 ----------
    rm_recent = ms.list_recent(limit=20, project_id="RM11")
    rm_ids = {e.get("id") for e in rm_recent}
    rm_titles = [e.get("title") or "" for e in rm_recent]

    assert rid_r1 in rm_ids, "RM11 视角应能看到 RM11 导入的条目"
    assert rid_f1 not in rm_ids, "RM11 视角不应看到 Fambase 导入的条目"
    assert any("RM11" in t for t in rm_titles), "应有 RM11 相关标题"
    assert not any("Fambase" in t for t in rm_titles), "RM11 列表里不应出现 Fambase 标题"

    rm_history = rh.list_run_records(limit=20, project_id="RM11")
    rm_demands = [r.get("demand_title") or "" for r in rm_history]
    assert any("RM11" in d for d in rm_demands), "RM11 历史中应有 RM11 生成记录"
    assert not any("Fambase" in d for d in rm_demands), "RM11 历史中不应出现 Fambase 生成记录"


def test_dual_project_search_and_entry_content(patch_storage):
    """同一流程下：搜索与 get_entry_content 均按 project_id 隔离。"""
    ms, _ = patch_storage

    # Fambase 搜「直播」应只命中 Fambase 条目
    fam_search = ms.search("直播", limit=10, project_id="FAMBASE")
    assert all(
        (e.get("project_id") or "FAMBASE").upper() == "FAMBASE" for e in fam_search
    ), "Fambase 搜索应只返回 FAMBASE 条目"
    if fam_search:
        assert any("Fambase" in (e.get("title") or "") for e in fam_search)

    # RM11 搜「风控」应只命中 RM11 条目
    rm_search = ms.search("风控", limit=10, project_id="RM11")
    assert all(
        (e.get("project_id") or "RM11").upper() == "RM11" for e in rm_search
    ), "RM11 搜索应只返回 RM11 条目"
    if rm_search:
        assert any("RM11" in (e.get("title") or "") for e in rm_search)

    # 全回归 source_id 按项目隔离：Fambase 与 RM11 各有 full_regression 槽位
    ms.add_entry_with_dedup(
        ms.TEST_CASES_SOURCE_TYPE,
        "Fambase 全回归内容",
        source_id="full_regression",
        title="全回归测试用例",
        summary="Fambase",
        project_id="FAMBASE",
    )
    ms.add_entry_with_dedup(
        ms.TEST_CASES_SOURCE_TYPE,
        "RM11 全回归内容",
        source_id="full_regression",
        title="全回归测试用例",
        summary="RM11",
        project_id="RM11",
    )

    c_fam = ms.get_entry_content(ms.TEST_CASES_SOURCE_TYPE, "full_regression", project_id="FAMBASE")
    c_rm = ms.get_entry_content(ms.TEST_CASES_SOURCE_TYPE, "full_regression", project_id="RM11")
    assert c_fam == "Fambase 全回归内容", "Fambase 的 full_regression 应为 Fambase 内容"
    assert c_rm == "RM11 全回归内容", "RM11 的 full_regression 应为 RM11 内容"
