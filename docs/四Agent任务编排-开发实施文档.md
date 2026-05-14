# 四 Agent 任务编排 · 开发实施文档

> **文档用途**：占位符实现方式、CrewAI 兼容说明、具体代码改造点及实施步骤。
>
> **创建日期**：2025-03-02

---

## 一、占位符与实现方式映射

| 占位符 | 实现方式 | 说明 |
|--------|----------|------|
| `{prd_content}` | `inputs = {"prd_content": demand}` | 将原有 `demand` 改为 `prd_content`，通过 CrewAI `kickoff(inputs=...)` 传入 |
| `{project_context}` | `desc.replace("{project_context}", project_context)` | 沿用现有逻辑，在 `_build_crew_from_config` 构建 Task 时替换 |
| `{task1_output}` | 依赖 CrewAI `context=[task1]` 或自定义执行循环中替换 | 若 CrewAI 的 context 能正确注入上游输出则用原生；否则采用顺序执行 + 手动替换 |
| `{task2_output}` | 同上 | 同上 |
| `{task3_output}` | 同上 | 同上 |

---

## 二、CrewAI 兼容说明

### 2.1 `{prd_content}` 通过 inputs 传入

- CrewAI 的 Task `description` 中可使用 `{variable_name}`，在执行时由 `inputs` 字典提供。
- 当前代码使用 `inputs = {"demand": llm_demand}`，task 中为 `{demand}`。
- **改造**：统一改为 `inputs = {"prd_content": demand}`，task description 中 `{demand}` 改为 `{prd_content}`。

### 2.2 `{taskN_output}` 的三种处理方式

| 方式 | 说明 | 适用场景 |
|------|------|----------|
| **A 原生** | 依赖 CrewAI 的 `Task(context=[task1])`，由框架自动将 task1 的输出注入到 task2 的上下文中 | CrewAI 版本支持且实际注入正确时 |
| **B 依赖 context** | 在构建 Task 时设置 `context=[task1]`，CrewAI 会将上游输出作为「前文」传给下游；需确认下游 description 中是否需显式 `{task1_output}` | 框架文档支持 context 传递时 |
| **C 自定义执行循环** | 不依赖 CrewAI 的 context，改为顺序执行：先跑 task1，取输出；再构建 task2 description，将 `{task1_output}` 替换为 task1 的实际输出，再跑 task2；依次类推 | 当 A/B 无法正确注入时，作为兜底方案 |

**结论**：若 CrewAI 的 context 不能正确注入上游输出，则需采用 **C 自定义执行循环**，在每次执行前替换对应占位符。

---

## 三、具体代码改造点

### 3.1 run_pipeline 的 inputs

**现状**（`crew_test.py`）：

```python
inputs = {"demand": llm_demand}
```

**改造后**：

```python
inputs = {"prd_content": llm_demand}
```

并确保 `config/agents.yaml` 中 task1 的 description 使用 `{prd_content}` 而非 `{demand}`。

### 3.2 _build_crew_from_config 扩展

**现状**：仅对 `{project_context}` 做 `replace`。

**改造**：增加对 `{prd_content}` 的处理。由于 `{prd_content}` 通过 `inputs` 传入，**无需**在 `_build_crew_from_config` 中替换；CrewAI 会在 kickoff 时注入。

对于 `{task1_output}`、`{task2_output}`、`{task3_output}`：

- **若采用 A/B**：依赖 CrewAI context，不在 build 阶段替换。
- **若采用 C**：不在 `_build_crew_from_config` 中处理，改为在自定义执行循环中，每次执行前对当前 task 的 description 做 `replace("{task1_output}", task1_result)` 等。

### 3.3 自定义执行循环（方式 C）伪代码

```python
# 顺序执行：task1 -> task2 -> task3 -> task4
task1_result = run_task(crew, task1, inputs={"prd_content": demand})
task2_desc = get_task_description(task2)
task2_desc = task2_desc.replace("{task1_output}", task1_result)
task2_result = run_task(crew, task2, inputs={"prd_content": demand, "task1_output": task1_result})
# ... 依此类推
```

实际实现时需根据 CrewAI API 确定如何对单个 Task 执行并获取输出。

---

## 四、config/agents.yaml 示例（任务描述与占位符保持一致）

```yaml
tasks:
  - id: task1
    agent_id: doubt_agent
    description: |
      请以【文档分析师】的身份，对传入的需求文档 {prd_content} 进行深度扫描，通过攻击性阅读找出潜藏漏洞。要求严格执行四大审查维度（模糊性、逻辑冲突、极端场景、隐性数据链路），并确保排版符合中英零空格规范。
      {project_context}
      需求文档：
      {prd_content}
    expected_output: "一份结构化的《需求风险评估报告》。以 Markdown 列表形式输出：
### 模块名称：[具体模块]
包含固定字段：【风险类型】、【风险概述】、【优先级建议】、【问题描述】、【建议/疑问】。若无明显问题，须列出至少3个最复杂的逻辑确认点。"

  - id: task2
    agent_id: organize_agent
    context: [task1]
    description: |
      请接收原始PRD {prd_content} 及《需求风险评估报告》 {task1_output}。将二者转化为测试点大纲。要求：必须吸收风险报告漏洞，严格执行“极简命名法”（绝无动词），绝对遵守中英零空格排版纪律。
      【task1 输出 - 需求风险评估报告】
      {task1_output}
      【需求文档】
      {prd_content}
      {project_context}
    expected_output: "请接收原始PRD {prd_content} 及《需求风险评估报告》 {task1_output}。将二者转化为测试点大纲。要求：必须吸收风险报告漏洞，严格执行“极简命名法”（绝无动词），绝对遵守中英零空格排版纪律。

Expected Output (预期输出):
一份分层的 Markdown 测试点大纲。层级为：### 主模块 -> #### 子场景 -> 具体的测试点。单行格式固定为：
* [测试点ID] | [用例概述] | [类型] | [优先级]
  * 验证逻辑补充：[一句话说明具体的验证逻辑，含状态与边界约束]。"

  - id: task3
    agent_id: case_agent
    context: [task2]
    description: |
      请接收《测试点大纲》 {task2_output}，将其扩充为9列标准测试用例表格。核心纪律：表头固定；前置条件仅写本条特有状态或数据，禁止用户/群主已登录、App已打开等默认基础前提；步骤执行【角色】《页面》格式；预期结果去动词化，多条同单元格「1.xxx；2.yyy」全角分号串联、单条禁「1.」，允许 `<br>`；排版100%零空格。
      【task2 输出 - 测试点骨架】
      {task2_output}
      {project_context}
    expected_output: "ask Description (任务描述):
请接收《测试点大纲》 {task2_output}，将其扩充为9列标准测试用例表格。核心纪律：表头固定；前置条件仅写本条特有状态或数据，禁止用户/群主已登录、App已打开等默认基础前提；步骤执行【角色】《页面》格式；预期结果去动词化，多条同单元格「1.xxx；2.yyy」全角分号串联、单条禁「1.」，允许 `<br>`；排版100%零空格。

Expected Output (预期输出):
纯净的 Markdown 表格格式测试用例。表头必须为：
| 序号 | 用例编号 | 主模块 | 子场景 | 用例概述 | 优先级 | 前置条件 | 测试步骤 | 预期结果 |
除表格外，不要输出任何多余的问候语或废话。"

  - id: task4
    agent_id: review_agent
    context: [task3]
    description: |
      你是一位“洁癖症晚期”的【顶级QA验收官/The Reviewer】。你的唯一任务是用最苛刻的标准审查测试用例（9列表格），发现任何瑕疵直接打回。
必须逐条核对四大验收标准：

排版视觉红线：中英/数字边界是否有空格（Fail）、英文内部是否丢失空格（Fail）。

字段规范审查：表头是否为9列；概述/结果是否混入过程动词；前置条件是否含废话或用户/群主已登录、App已打开等无意义默认前提；步骤是否含【角色】《页面》符号；预期结果多条是否为「1.xxx；2.yyy」分号串联（允许 `<br>`）。

逻辑覆盖审查：溯源2号Agent大纲，检查是否遗漏异常防劣化场景（断网、杀进程等）。

冗余审查：剔除无意义用例，指出未做异常拆分的臃肿用例。
      【task3 输出 - 测试用例】
      {task3_output}
    expected_output: "审查结果报告。
若有违规，按格式列出明细：【用例ID】+【错误类型】+【原内容】+【修改建议】。
若100%完美，仅输出唯一指令：「✅ 通过审查，用例符合 Fambase 交付标准。」"
```

---

## 五、9 列新表头与表格解析/导出的兼容说明

### 5.1 当前表头（8 列）

| 用例ID | 模块 | 场景 | 用例概述 | 优先级 | 前置条件 | 操作步骤 | 预期结果 |

### 5.2 若扩展为 9 列

若业务需要新增列（如「依赖模块」「备注」等），需同步调整：

1. **config/agents.yaml**：case_agent 的 backstory 与 task3 的 description 中表头描述。
2. **_parse_markdown_tables**：解析逻辑基于 `|` 分隔，列数自动适配，**无需修改**。
3. **_export_to_excel**：按行写入，列数自动适配，**无需修改**。
4. **Excel 下载**：列宽与表头展示由 openpyxl 默认处理，通常无需改动。

结论：表格解析与导出按「行 + 列」结构处理，列数变化不会破坏现有逻辑，只需保证 Agent 输出的 Markdown 表格格式正确（`|` 分隔、表头与分隔行完整）。

---

## 六、实施步骤与占位符可用性检查清单

### 6.1 实施步骤

1. 修改 `config/agents.yaml`：将 task1 的 `{demand}` 改为 `{prd_content}`；task2–task4 按需求文档补充 `{task1_output}`、`{task2_output}`、`{task3_output}`。
2. 修改 `crew_test.run_pipeline`：`inputs = {"prd_content": llm_demand}`。
3. 验证 CrewAI context 是否注入上游输出：
   - 若 task2 能正确拿到 task1 的结果，则无需改动执行逻辑。
   - 若不能，则实现自定义顺序执行 + 占位符替换（方式 C）。
4. 回归测试：跑一次完整流水线，检查 task1–task4 输出是否符合预期，Excel 导出是否正常。

### 6.2 占位符可用性检查清单

| 占位符 | 检查项 | 验证方式 |
|--------|--------|----------|
| `{prd_content}` | 在 task1 的 description 中能被替换为实际需求文本 | 查看 task1 输出是否基于传入的需求内容 |
| `{project_context}` | 在 task1–task3 的 description 中被正确替换 | 输出中应体现项目背景/知识库摘要 |
| `{task1_output}` | 在 task2 的 description 中被替换为 task1 的实际输出 | 查看 task2 输出是否引用了风险报告内容 |
| `{task2_output}` | 在 task3 的 description 中被替换为 task2 的实际输出 | 查看 task3 输出是否基于测试点骨架 |
| `{task3_output}` | 在 task4 的 description 中被替换为 task3 的实际输出 | 查看 task4 审查是否针对实际用例表格 |
