# -*- coding: utf-8 -*-
"""Agent 知识库构建：project_id 与路径规则单测"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_build_agent_knowledge_writes_rm11_paths_when_empty_sources(monkeypatch, tmp_path):
    """无记忆源时走最小模板分支，应写入 RM11 专属文件名（不调用 LLM）。"""
    import agent_knowledge_service as aks

    monkeypatch.setattr(aks, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(aks, "_get_raw_content_for_knowledge", lambda project_id=None: "")
    monkeypatch.setattr(aks, "_load_project_memory", lambda project_id=None: "")

    ok, err = aks.build_agent_knowledge(gemini_key="dummy-key-for-minimal-branch", project_id="RM11")
    assert ok is True
    assert err == ""

    md_path = tmp_path / "agent_knowledge_rm11.md"
    meta_path = tmp_path / "agent_knowledge_rm11_meta.json"
    assert md_path.is_file()
    assert meta_path.is_file()
    text = md_path.read_text(encoding="utf-8")
    assert "Agent 知识库" in text
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert "last_updated" in meta


def test_build_agent_knowledge_fambase_default_file_when_empty_sources(monkeypatch, tmp_path):
    import agent_knowledge_service as aks

    monkeypatch.setattr(aks, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(aks, "_get_raw_content_for_knowledge", lambda project_id=None: "")
    monkeypatch.setattr(aks, "_load_project_memory", lambda project_id=None: "")

    ok, _ = aks.build_agent_knowledge(gemini_key="dummy", project_id="FAMBASE")
    assert ok
    assert (tmp_path / "agent_knowledge.md").is_file()
    assert (tmp_path / "agent_knowledge_meta.json").is_file()


def test_build_agent_knowledge_no_key_returns_false(monkeypatch):
    import agent_knowledge_service as aks

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    ok, err = aks.build_agent_knowledge(gemini_key="", project_id="RM11")
    assert ok is False
    assert "GEMINI" in err or "配置" in err
