# 页面切换保留输入 · 代码 Review 问题清单

> 依据文档：`docs/页面切换保留输入-PRD.md`、`docs/页面切换保留输入-开发实施文档.md`  
> Review 目标文件：`app_ui.py`

---

## 一、结论摘要

当前实现方向正确（已引入 persist 机制并覆盖多个模块），但存在以下关键问题：

1. persist 辅助函数出现重复定义，后定义覆盖前定义，导致项目作用域逻辑失效。
2. 设置页将 API Key 纳入了 persist，违反「敏感字段不在本期范围」约束。
3. 多项目隔离未生效，需求输入草稿可能在项目间串用。
4. 用户主动清空时未清理 persist 草稿，可能出现“清空后又恢复旧内容”。
5. 部分“草稿优先展示”场景缺少未保存提示文案，和 PRD 规范仍有差距。

---

## 二、详细问题

## 1) persist 函数重复定义，作用域实现被覆盖（高优先级）

- 在 `app_ui.py` 前段已实现带 `project_scoped` 的函数：
  - `_get_persist_key()`
  - `_restore_widget_state(widget_key, project_scoped=True)`
  - `_persist_widget_state(widget_key, project_scoped=True)`
  - `_clear_persist_widget_state(...)`
- 但在中段又定义了同名简化版：
  - `_restore_widget_state(widget_key, default=None)`
  - `_persist_widget_state(widget_key)`
- Python 以后定义为准，导致前段带项目作用域的实现失效。

**影响：**
- 与实施文档「按 `current_project` 命名空间隔离草稿」不一致。
- 后续调用即使希望做项目隔离，也不会生效。

---

## 2) 设置页 API Key 被持久化（高优先级）

- 代码对 `settings_gemini_key` 使用了 `_restore_widget_state` 与 `on_change` 持久化。
- `_init_settings_persist_from_defaults()` 也会将 `gemini_key` 写入 persist。

**与需求冲突：**
- PRD 与实施文档均明确：设置页敏感字段（Key）不纳入本期页面切换保留输入。

**影响：**
- 超出本期安全边界，可能带来敏感信息保留风险。

---

## 3) 多项目输入隔离未落地（高优先级）

- 当前生效的是无 `project_scoped` 版本 persist 逻辑，persist key 形如 `persist_ui_run_paste_content`。
- 未按项目区分（如 `persist_ui_FAMBASE_xxx` / `persist_ui_RM11_xxx`）。

**影响：**
- 需求输入型模块（生成用例、项目记忆、用例补充、文档问答等）存在跨项目串草稿风险。
- 不满足 PRD「同项目内保留、跨项目隔离」要求。

---

## 4) 清空/结束动作未清理 persist（中高优先级）

- `_clear_persist_widget_state()` 已定义，但暂无调用落点。

**影响：**
- 用户点击“清空”“结束对话”后，widget 虽清空，但 persist 仍留旧值；
- 下次进入页面会恢复旧值，违反 PRD 的 P0-AC-3。

---

## 5) 草稿提示文案缺失（中优先级）

- PRD 要求：对有显式保存动作区域，若显示的是未保存草稿，应有明确提示文案（配置化，不硬编码）。
- 目前主要实现了“草稿优先显示”，但提示文案基本缺失。

**影响：**
- 用户可能误以为数据已保存到持久层，产生认知偏差。

---

## 三、建议修复方案（优先级顺序）

### P0（本次建议必须修）

1. **统一 persist helper 实现**
   - 删除重复定义，保留一套统一函数签名（含 `project_scoped`）。
2. **落实项目作用域**
   - 需求输入型模块默认 `project_scoped=True`。
   - 设置页非敏感字段可 `project_scoped=False`（全局草稿）。
3. **移除敏感字段 persist**
   - `settings_gemini_key` 不 restore / persist。

### P1（建议尽快补齐）

4. **用户主动清空时同步清理 persist**
   - 在对应按钮处理逻辑调用 `_clear_persist_widget_state(...)`。
5. **补充“未保存草稿”提示文案**
   - 文案走 `config/ui_texts.yaml`，避免硬编码。

---

## 四、验收建议（针对本次问题）

1. FAMBASE 输入生成用例草稿 A，切到 RM11 同模块，不应自动带入 A；切回 FAMBASE 仍应看到 A。
2. 设置页修改模型未保存，切页再回仍保留；API Key 不做跨页草稿恢复。
3. 点击清空后切页再回，不应恢复被清空内容。
4. 对有保存动作的区域，确认“未保存草稿”提示存在且文案来自配置。

---

## 五、备注

本清单为本次代码现状与 PRD/实施文档对照结果，供后续修复与测试回归使用。
