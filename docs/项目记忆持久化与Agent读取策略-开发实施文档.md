# 项目记忆持久化与 Agent 读取策略 · 开发实施文档

> **依赖需求**：`docs/项目记忆持久化与Agent读取策略.md`、`docs/Agent知识库-PRD.md`（含 AC3b 全回归导入后自动刷新）、`docs/双项目工作台-项目记忆与Fambase兼容要点.md`。  
> **非本文范围**：`docs/数据持久化与自动备份-方案.md`（冷备/恢复另案实施）。  
> **更新时间**：2026-04-02

---

## 一、目标与当前缺口

### 1.1 产品目标（摘要）

| 项 | 要求 |
|----|------|
| 全回归导入后 | **成功且校验通过、且聚合视图实际更新**后，**自动**执行一次 Agent 知识库构建（与手动「刷新知识库」同一逻辑） |
| 双项目 | 构建与读写必须绑定 **当前 `project_id`**，与 `FAMBASE` / `RM11` 文件隔离一致 |
| 手动刷新 | 保留「刷新知识库」按钮，作为补刷/重试 |
| 7 天自动过期 | 保持现有「进入项目记忆页 + 过期则自动构建」逻辑；全回归触发的构建应 **更新 `last_updated`**，从而重置 7 天计时 |

### 1.2 代码现状（截至文档编写时）

| 能力 | 位置 | 现状 |
|------|------|------|
| 全回归导入 | `app_ui.py` → `_handle_full_regression_import` | 写存档、合并聚合、`mark_context_cache_dirty`、聚合条摘要；**未**调用 `build_agent_knowledge` |
| 知识库构建 | `agent_knowledge_service.py` → `build_agent_knowledge` | 读 `memory_entries` + `project_memory`，写 `agent_knowledge(.md)` + meta；**`project_id` 仅通过环境变量 `APP_CURRENT_PROJECT` 推断** |
| Agent 上下文 | `crew_test.py` → `get_project_context_for_agent` | 若 `load_agent_knowledge` 非空则拼「知识库」；否则回落 `get_recent_for_agent` + `full_regression` 聚合 |
| 项目记忆页自动刷新 | `app_ui.py` → `_render_module_memory` | `is_knowledge_stale` 时最多自动跑一次（`kb_auto_done` session 标记） |

**结论**：需在 **全回归导入成功路径** 上 **显式接入** 知识库构建，并保证 **`project_id` 与当前工作台一致**。

---

## 二、推荐改动清单

### 2.1 `agent_knowledge_service.build_agent_knowledge`（建议）

**问题**：仅依赖 `os.environ.get("APP_CURRENT_PROJECT")` 易在异步/子调用中漏设，不利于测试与复用。

**建议签名**（向后兼容）：

```python
def build_agent_knowledge(
    gemini_key: str = "",
    gemini_model: str = "",
    project_id: str | None = None,
) -> tuple[bool, str]:
```

- 若 `project_id` 为 `None`，再回退到 `_normalize_project_id(os.environ.get("APP_CURRENT_PROJECT"))`。
- 内部凡依赖项目的路径（`_get_raw_content_for_knowledge`、`_get_knowledge_paths`、`_load_project_memory`）一律使用解析后的 `pid`。

**验收**：单测可传入 `project_id="RM11"`，断言写入 `agent_knowledge_rm11.md`（与现有路径规则一致）。

### 2.2 `app_ui.py` → `_handle_full_regression_import`

**触发条件**（与 PRD 一致）：

1. 聚合写入 **`add_entry_with_dedup` 返回的 `status != "skipped"`**（即聚合内容相对库中确有更新）。  
2. 若 `status == "skipped"`（`st.info`「导入内容与现有聚合结果一致…」分支）：**不**触发知识库重建（无新数据，避免浪费 Token）。

**不推荐**在「仅写入存档 `full_regression:{id}` 但聚合未变」的路径触发（当前逻辑在聚合 skipped 时已 `return`，无需额外判断存档）。

**调用方式**：

- 在成功提示 `st.success(...)` **之前**（或之后与 `st.rerun` 前），执行：
  - `with st.spinner(...)`：文案走 `config/ui_texts.yaml`（建议新增 key，如 `memory_tab.knowledge_refresh_after_regression`）。
  - `build_agent_knowledge(gemini_key=..., gemini_model=..., project_id=project_id)`。
- **失败策略**（对齐 `Agent知识库-PRD`）：**不覆盖**旧 `agent_knowledge.md`；`st.warning` 提示用户可手动「刷新知识库」；**不**阻断「导入已成功」的主结论（摘要失败同理）。

**`project_id`**：直接使用函数参数 `project_id`（与 `_render_module_memory` 中 `_get_current_project()` 传入值一致），**不要**依赖调用时全局 env 是否已设置。

### 2.3 与「进入页面 7 天自动刷新」的竞态

可能出现：同一次页面生命周期内，先因 **全回归导入** 触发构建，又因 **`is_knowledge_stale` + `kb_auto_done`** 再触发一次。

**建议**（择一，按实现成本）：

- **方案 A（推荐）**：全回归导入成功后 **`st.rerun()`** 前已完成构建 → 刷新后 `last_updated` 为当前时间 → `is_knowledge_stale` 为 False → 第二次不再触发。  
- **方案 B**：在 `st.session_state` 设置 `kb_auto_done` 或「本 run 已构建」标记，避免同 run 内重复 LLM。

### 2.4 环境变量 `APP_CURRENT_PROJECT`

`crew_test.get_project_context_for_agent` 与部分路径仍依赖 **`APP_CURRENT_PROJECT`**。在 Streamlit 主流程中应保证 **生成用例前** 已设置（现有工作台若已设置可不改）。全回归导入路径若 **显式传入 `project_id` 给 `build_agent_knowledge`**，则 **不强制**依赖 env 完成知识库文件写入；但为一致性，仍建议在 `_render_main_app` 或运行流水线入口处 **统一** `os.environ["APP_CURRENT_PROJECT"] = project_id`（若尚未如此）。

---

## 三、设计图「最新」与 DB SoT（实现核对）

需求：**列表/注入上下文时按 DB 字段排序，禁止模型推断「最新」**。

**核对点**：

- `memory_store.list_recent` / 设计图历史列表的 **SQL `ORDER BY`** 是否为 `created_at DESC` 或单调 `id DESC`。  
- 任何将「多张设计图」拼进 prompt 的路径，必须先 **排序再拼接**。  
- 若 UI 仍展示无序列表，需在产品验收前修正。

（具体函数以当前 `memory_store` 与设计图导入实现为准，本文不绑定行号。）

---

## 四、测试建议

| 编号 | 场景 | 期望 |
|------|------|------|
| T1 | `FAMBASE` 下导入全回归（聚合有新增） | `build_agent_knowledge` 被调用且 `project_id=FAMBASE`；`agent_knowledge.md` 更新 |
| T2 | 同上，`RM11` | 写入 `agent_knowledge_rm11.md` |
| T3 | 导入内容与聚合完全一致（`status==skipped`） | **不**调用构建（或调用次数 0） |
| T4 | 构建失败（mock LLM 抛错） | 旧知识库文件仍在；用户看到 warning；导入成功文案仍可出现 |
| T5 | 无 Gemini Key | 与手动刷新一致：失败提示，不覆盖旧文件 |

**单测**：对 `build_agent_knowledge(..., project_id="RM11")` 做轻量 mock（不写真实 Key），断言目标路径与 meta 写入。

**手工**：导入后无需点击「刷新知识库」，生成用例上下文应反映新全回归（在知识库模式下以 `load_agent_knowledge` 为准）。

---

## 五、文案与配置

在 `config/ui_texts.yaml` 的 `memory_tab` 下建议新增（键名可按文案规范微调）：

- 全回归后自动构建中的 spinner 文案；  
- 全回归后自动构建失败时的 `warning` 模板（可复用 `knowledge_refresh_fail` 或单独 key）。

禁止在业务逻辑中硬编码长句（遵守 `project-standards.mdc`）。

---

## 六、与未来架构（Postgres + R2）的关系

当前实现以 **本地 `config/*.md` + memory_store（Sqlite/JSON）** 为准。迁移到 **Postgres + R2** 后：

- **接口层**：保留「全回归导入成功 → 触发一次记忆制品重建」的 **钩子**（可仍为 `build_agent_knowledge` 或替换为服务层函数）。  
- **读取契约**：`get_project_context_for_agent` 仍应优先 **制品 C**，与 `项目记忆持久化与Agent读取策略.md` 第三节一致。

---

## 七、关联文件（实现时必打开）

| 文件 | 说明 |
|------|------|
| `app_ui.py` | `_handle_full_regression_import`、`_render_module_memory` |
| `agent_knowledge_service.py` | `build_agent_knowledge`、路径与 meta |
| `crew_test.py` | `get_project_context_for_agent` |
| `memory_store.py` | `TEST_CASES_SOURCE_TYPE`、`add_entry_with_dedup`、`get_entry_content` |
| `config/ui_texts.yaml` | 文案 |

---

## 八、验收映射（研发自测）

| PRD / 策略文档 | 验收项 | 本文第二节 |
|----------------|--------|------------|
| `Agent知识库-PRD` AC3b | 全回归导入成功后自动构建 | §2.2 |
| `项目记忆持久化与Agent读取策略` §2.2 | 自动触发 + 按钮仅补刷 | §2.2、§2.3 |
| `项目记忆持久化与Agent读取策略` §4 | `project_id` 隔离 | §2.1、§2.2、T2 |
| `项目记忆持久化与Agent读取策略` §2.3 | 设计图最新 DB SoT | §三 |
