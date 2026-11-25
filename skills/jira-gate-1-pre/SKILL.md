---
name: jira-gate-1-pre
description: Jira 上游需求 G1-Pre 向下流转门禁。当用户给出 Jira 链接或单号并要求"检查原始诉求是否清楚 / 能不能进入需求澄清或处理中 / 做 G1-Pre 预检"时使用。只处理类型为「客户用户故事」「故事」「项目概算评估」「任务」「项目实施阶段」的单子，先判断事实、诉求与期望、依据三项检查，再按功能/效果/技术评估/迁移压测/研究文档/项目任务六类需求套用观察重点，只输出「通过 / 不通过」；不通过时先列 N 个阻断项、再按固定分类给出当前问题、依据与最小补充动作并 @ 报告人；通过时生成「需求评估摘要」（置顶由接入层完成）。本技能只读单据、只写 G1-Pre 评论（最新评论正文一致时不重复），不流转状态、不修改字段、不上传或删除附件。
metadata:
  short-description: Jira 上游需求 G1-Pre 向下流转门禁
---

# Jira 上游需求 G1-Pre 向下流转门禁

用户给一个 Jira 链接，你读取标题、描述、字段、附件、外部链接和必要评论，判断
**上游提交人是否已经把需求对象、当前问题、诉求、期望和必要证据说清楚，使下一接手方能够继续处理**，
给出 `通过` / `不通过` 结论，并把结论写入这张单子的评论。

**规则版本**：`V1.2`（contract_version `1.0`，tools_version `V2.3.1`，2026-11-25 更新）。

## 交付物

本技能只有一个写入型交付物：**G1-Pre 评论**（最新一条 G1-Pre 评论正文与本轮一致时不再重复，
结论或摘要变化后才写新评论）。

1. Jira 评论：通过时写「标题 + 需求评估摘要（背景 / 目标 / 要点 / 建议澄清方向）+ 免责声明」；
   不通过时先列 N 个阻断项摘要，再逐项给出当前问题、依据与最小补充动作，并在 `*报告人：*` 行
   用 Jira 用户 key `@报告人`。
2. 对话摘要：单据类型 + 需求类型 + 结论 + 关键阻断项 + 评论 ID / 是否因幂等跳过 + 未完成原因。
3. 机器可读结果：任务根目录 `.skill-result.json`，用于平台统计。

**本技能不生成正式报告文档，不上传附件，不删除附件，不流转状态，不修改任何 Jira 字段。**
本地只允许在 `work/jira-gate/<KEY>/` 下存放附件与解析中间文件，交付完成后清理。

## 与规则文档的落地差异

规则文档假设存在 Jira 接入层在 transition 前调用本技能，并由接入层决定是否放行。本仓库没有该拦截层，
因此按以下方式落地：

- **本技能不做 transition**：只输出结论与评论，调用方拿到 `conclusion` 后自行决定是否继续原 transition。
- **评论由本技能写入**：`dry_run=true` 时只返回拟写评论，不写 Jira。
- **幂等通过比对最新评论正文实现**：评论中不写规则版本、内容指纹或 sha256；本轮渲染结果与最新一条
  G1-Pre 评论正文一致时不重复评论。
- **接入层未传 transition 时**：按问题类型推断预期目标状态并记录 `transition.provided=false`；
  transition 上下文只保存在 `result.json` 或接入层元数据中，绝不据此执行流转。
- **置顶**：本技能不调用置顶接口，`result.json` 的 `pinStatus` 固定写 `pending`；置顶由接入层完成。
- **`not_applicable` 不写评论**：`applicability=not_applicable` 时只返回分流理由，不写门禁评论。
- **执行异常不写内容结论**：`executionStatus=execution_error` 时不写「通过 / 不通过」评论，由平台重试。
- **类型过滤**：只认 Jira 的 `issuetype`，五种类型之外直接跳过；不要用标题、标签或描述猜测类型。
- **需求设计后的内部流转不重复检查**：`客户用户故事` / `故事` 在显式传入目标状态 `评审中` 时直接跳过。

## 输入

必需输入：

- `jira_url`：Jira 链接或单号。

可选输入（来自任务提示词的 `{inputs_lines}`；缺失时按默认值处理）：

- `transition_id`：接入层实际触发的 transition ID。
- `source_status`：触发前状态；默认取单据当前状态。
- `target_status`：允许继续的目标状态；默认按问题类型推断。
- `dry_run`：`true` 时只判定并生成评论，不写 Jira。

五种问题类型的预期 transition：

| 问题类型 | 动作 | 目标状态 |
|---|---|---|
| 客户用户故事 | 跟进需求 | 需求澄清 |
| 故事 | 跟进需求 | 需求澄清 |
| 项目概算评估 | 开始评估 | 处理中 |
| 任务 | 开始进行 | 处理中 |
| 项目实施阶段 | 开始评估 | 处理中 |

`客户用户故事` / `故事` 进入 `需求澄清` 后，`需求设计 → 评审中` 属于产品设计完成后的内部流转，
不再重复执行 G1-Pre；只有接入层显式传入目标状态时才据此跳过，不做猜测。

## 判定依据

判定标准以 [references/g1-pre-rules.md](references/g1-pre-rules.md) 为准，**先读它再开始判定**。核心口径：

取数、附件解析与写评论的接口细节见 [references/jira-api.md](references/jira-api.md)。

- 必须依次完成三项检查：`facts`（事实是否清楚）、`request`（诉求与期望是否清楚）、
  `evidence`（依据是否可信）。三项无阻断才 `pass`，任一项有阻断即 `fail`。
- 判定前先确定 `requirementKind`，六选一：`functional_change`、`defect_or_effect`、
  `technical_assessment`、`migration_or_load_test`、`research_or_document`、`project_task`。
  类型只改变观察重点，不建立第二套结论。
- 不得因未使用模板、章节名称不同或没有 BadCase 而退回；`structured` 与
  `partially_structured` 都可通过。
- 不得仅凭标题或一句口号通过。只有标题、口号、单句概括，或只有「详见附件」而正文没有核心事实，
  归 `unstructured_description`。
- 产品归属、截止日期、问题发现产品、业务价值、优先级、Story Points、技术方案等字段缺失**不阻断**，
  只写进「建议澄清方向」。
- 多诉求逐项判定；只阻断缺失项，不因数量多退回。
- 每条阻断问题只归一个主分类，分类枚举见规则文档第 7 节。
- 「方案未定」「待决策」「技术实现未知」不阻断；「未指定接手角色」不得作为 finding。

---

## 执行流程

### 0. 类型预检（必须最先执行）

```bash
node <技能目录>/scripts/g1-pre.mjs precheck "<JIRA_URL>" \
  [--transition-id "<ID>"] [--source-status "<源状态>"] [--target-status "<目标状态>"]
```

- `supported=true`：继续第 1 步。
- `supported=false`：立即停止。不读附件、不判定、不写评论，`.skill-result.json` 的 `conclusion`
  写「不适用」，只在对话里说明跳过原因（类型不属于五类上游事项，或目标状态是 `评审中` 的内部流转）。
- 若单据类型属于五类之一，但明显是会议、出差、考勤、运维、权限申请等事务性记录，按规则文档 §2.2
  由接入层过滤；本技能仍按正文实际内容判定，不凭标题关键词自行豁免。

### 1. 读取单据上下文

```bash
node <技能目录>/scripts/g1-pre.mjs context "<JIRA_URL>" --description-limit 20000 --comments 10 \
  [--transition-id "<ID>"] [--source-status "<源状态>"] [--target-status "<目标状态>"]
```

输出包含标题、完整描述、类型、状态、报告人及 `[~name]` 写法、自定义字段、附件清单（含作者与创建时间）、
最近评论与 transition 上下文。

取数要点：

- 先以标题和描述建立原始诉求；字段用于补充对象与客户；附件用于补充过程、样例、日志和指标。
- 评论只能补充事实，**不能替代上游原始诉求**；未声明「以此为准」的冲突评论不得直接采信。
- 如果描述只有「详见附件」「看截图」「看日志」「与某人沟通后处理」，即使附件完整，也判 `不通过` /
  `unstructured_description`。

### 2. 下载并解析附件

仅有附件时才需要执行；没有附件直接进入第 3 步。

```bash
node <技能目录>/scripts/jira-cli.mjs attachments "<JIRA_URL>" "work/jira-gate/<KEY>/attachments"
python <技能目录>/scripts/extract.py "work/jira-gate/<KEY>/attachments" --out "work/jira-gate/<KEY>/attachments.txt"
```

- 附件下载失败、压缩包解压失败或文档解析失败时，如实记为「附件不可读」；**不得当成「附件没有内容」，
  不得臆测包内内容，不得声称已核验**。
- 正文明确依赖的核心附件不可读 → `executionStatus=execution_error`，`executionStage=attachment_parse`，
  不写通过/不通过评论。
- 补充附件不可读只写建议，不改变结论。
- 正文声称提供截图、日志或规则材料，而对应材料实际缺失或只有空模板 → `不通过` /
  `required_evidence_missing`。
- 图片附件用图像查看能力查看；无法查看时如实说明，不猜内容。
- 引用证据要写到附件内部路径，例如 `附件.zip/PRD.md §4.2`。

### 3. 建立材料来源图

为每个参与判定的材料记录 `sourceRole`，取值见规则文档 §3.1：
`original_requirement` / `supporting_fact` / `technical_response` / `decision_record` /
`historical_review` / `other_reference` / `unknown`。

- 作者或时间未知时记 `unknown`，**不得用 unknown 材料否定原始诉求**。
- 后续研发、测试提供的方案、资源表、压测结果属于 `technical_response`，不是原始需求。
- 评论中声明「更正 / 以此为准 / 替代前文」时才可作为当前事实，并记录替代范围。

### 4. 解析需求表达

按语义识别四类信息，允许同义标题和合并表达：

```text
background     需求背景 / 业务场景 / 为什么提出
problem        问题说明 / 当前现状 / 当前现象与影响
expectation    需求期望 / 期望结果 / 本期范围
environment    客户环境 / 版本号 / 部署环境
```

输出：

- `structureStatus`：`structured` / `partially_structured` / `unstructured`；
- `structureEvidence`：能映射关键事实的结构证据；
- `specCoverage`：四个字段分别取 `present` / `partial` / `missing` / `not_applicable`。

`specCoverage` 不直接决定结论；`environment` 缺失只有在会改变处理对象、方案或验证时才可能阻断。

### 5. 完成三项检查

对每项检查分别给出 `pass` / `fail`，并写真实证据：

| id | 检查 | 通过边界 |
|---|---|---|
| `facts` | 事实是否清楚 | 能识别需求对象、应用场景、当前结果或问题、问题影响；类型化观察重点中的对象、环境和限制足以定位工作 |
| `request` | 诉求与期望是否清楚 | 能识别希望改变、解决、验证或产出什么；允许多个候选目标与待决问题 |
| `evidence` | 依据是否可信 | 核心证据可读可追溯、来源角色可判断、同层同版本无互斥定义、更正关系明确 |

按 `requirementKind` 套用规则文档 §5.2 的观察重点：

- `functional_change`：使用场景 + 当前结果或问题 + 改变范围 + 期望结果；不要求产品最终规则或交互稿。
- `defect_or_effect`：业务场景 + 至少一个可追溯样例（输入 / 当前输出 / 期望输出）+ 影响或差距。
- `technical_assessment`：评估对象 + 规模环境版本限制 + 当前问题或约束 + 期望评估结果；不要求技术方案。
- `migration_or_load_test`：迁移或压测对象 + 源与目标 + 数据环境条件 + 期望验证结果 + 声称依赖的材料可读。
- `research_or_document` / `project_task`：任务目标 + 范围对象 + 期望产出 + 必要背景和限制。

判定纪律：

- 证据不足、语义冲突或无法唯一判断，一律按不满足处理；但只有**同层同版本**的互斥定义才算冲突。
- 不把「类型不匹配」「字段缺失」「模板格式」列为阻断分类；类型不匹配只写建议。
- 同一根因只列一条 finding；不同诉求的独立缺口分别保留。
- 结构问题和具体事实缺口同时存在时，只选更根本的主要分类，通常优先 `unstructured_description`。

### 6. 写判定结果文件

在 `work/jira-gate/<KEY>/result.json` 写下结构化结果。字段名统一使用驼峰形式，
`g1-pre.mjs comment` 据此渲染评论，不接受其它写法：

不通过示例：

```json
{
  "schemaVersion": "1.2",
  "issueKey": "KEY-123",
  "issueType": "故事",
  "applicability": "requirement",
  "conclusion": "fail",
  "requirementKind": "functional_change",
  "structureStatus": "partially_structured",
  "structureEvidence": ["描述分段列出当前现象与期望能力，但没有独立的客户环境段"],
  "specCoverage": {
    "background": "present",
    "problem": "present",
    "expectation": "partial",
    "environment": "not_applicable"
  },
  "checks": [
    {
      "id": "facts",
      "status": "pass",
      "summary": "已写清客户、导出场景与当前报错结果",
      "evidenceRefs": ["description:paragraph-1"]
    },
    {
      "id": "request",
      "status": "fail",
      "summary": "只写希望优化导出，没有可识别的期望结果",
      "evidenceRefs": ["description:paragraph-2"]
    },
    {
      "id": "evidence",
      "status": "pass",
      "summary": "截图与描述一致，可核验报错现象",
      "evidenceRefs": ["attachment:10001"]
    }
  ],
  "findings": [
    {
      "id": "F1",
      "category": "unverifiable_target",
      "checkId": "request",
      "title": "期望结果不可验证",
      "currentProblem": "描述只写希望导出更快，未说明期望达到什么结果。",
      "evidenceRefs": ["description:paragraph-2"],
      "minimalAction": "请补充期望的导出结果，例如时限、条数或可用性要求。",
      "impact": "verification"
    }
  ],
  "assessmentSummary": null,
  "executionStatus": "success",
  "executionStage": "completed",
  "reporterMention": "[~zhangsan]",
  "ruleVersion": "V1.2",
  "contractVersion": "1.0"
}
```

通过示例（必须给出 `assessmentSummary`）：

```json
{
  "issueKey": "KEY-123",
  "applicability": "requirement",
  "conclusion": "pass",
  "requirementKind": "technical_assessment",
  "checks": [
    { "id": "facts", "status": "pass", "summary": "…", "evidenceRefs": ["description:paragraph-1"] },
    { "id": "request", "status": "pass", "summary": "…", "evidenceRefs": ["description:paragraph-2"] },
    { "id": "evidence", "status": "pass", "summary": "…", "evidenceRefs": ["description:paragraph-1"] }
  ],
  "findings": [],
  "assessmentSummary": {
    "background": "客户在语音 IVR 场景使用 200 路并发，当前部署无法满足扩容需要。",
    "goals": ["评估 200 路并发的资源配置", "给出可执行的部署建议"],
    "points": ["现有部署为单机双卡", "客户希望本月完成容量评估"],
    "clarifications": [
      {
        "question": "是否需要同时给出压测方案",
        "reason": "客户只提出资源评估，压测范围尚未说明",
        "impact": "scope"
      }
    ],
    "evidenceRefs": ["description:paragraph-1"]
  },
  "executionStatus": "success",
  "ruleVersion": "V1.2",
  "contractVersion": "1.0"
}
```

写入前自检：

- `conclusion` 只有 `pass` / `fail`；`fail` 时 `findings` 非空，`pass` 时 `findings` 为空。
- `checks` 必须覆盖 `facts`、`request`、`evidence` 三项，`status` 只有 `pass` / `fail`。
- 每条 finding 必须包含 `category`（第 7 节枚举）、`checkId`（三选一）、`currentProblem` 和
  `minimalAction`；`impact` 取 `scope` / `solution` / `verification` / `routing` / `unknown`。
- `pass` 时必须给出 `assessmentSummary`，且至少包含 `background`、`goals`、`points`；
  `clarifications` 可为空数组；`fail` 时 `assessmentSummary` 必须为 `null`。
- `contentHash` 为可选字段：算出来后回填完整 `sha256:...`；存在时必须是真实值，不要手填占位值。
  它只供接入层幂等与审计使用，不写入评论。
- `advisoryFindings` / `fieldHints` 为可选的非阻断记录字段，不参与结论，也不写入评论；
  产品归属、截止日期等缺失提示如需保留，写在这里供接入层审计即可。

### 7. 计算内容摘要并判重

附件已下载时**必须带 `--dir`**，让摘要覆盖附件的 sha256；否则只覆盖附件元数据。

```bash
node <技能目录>/scripts/g1-pre.mjs hash "<JIRA_URL>" --dir "work/jira-gate/<KEY>/attachments" \
  [--transition-id "<ID>"] [--source-status "<源状态>"] [--target-status "<目标状态>"]
```

把输出里的 `contentHash` 写回 `result.json` 的 `contentHash`（可选），然后判重：

```bash
node <技能目录>/scripts/g1-pre.mjs history "<JIRA_URL>" --file "work/jira-gate/<KEY>/result.json"
```

- `duplicate=true`：最新一条 G1-Pre 评论正文与本轮渲染结果一致。**不重复写评论**，
  在对话里说明「结论与摘要未变化，沿用评论 <ID>」，直接进入第 9 步。
- `duplicate=false`：继续第 8 步。
- 标题、描述、附件内容或参与判定的字段变化后，应重新判定；只要渲染出的评论正文与最新评论不同，
  就写新评论。
- 评论中不显示 `contentHash`、`ruleVersion`、sha256 或「G1-Pre 指纹」；这些信息只保留在
  `result.json` 与接入层元数据中。

### 8. 渲染并写入评论

先渲染预览，确认格式：

```bash
node <技能目录>/scripts/g1-pre.mjs comment --file "work/jira-gate/<KEY>/result.json"
```

`dry_run=true` 时到此为止：把 `body` 原样展示在对话里，**不执行 `--write`**。

非 dry_run 时写入：

```bash
node <技能目录>/scripts/g1-pre.mjs comment --file "work/jira-gate/<KEY>/result.json" --write
```

- 写入成功：`written=true`，记录 `commentId`。
- 幂等跳过：`skipped=true`，记录 `duplicateOf`。
- `executionStatus=execution_error`：脚本拒绝渲染通过/不通过评论，`written=false`、
  `skippedReason=execution_error`。按执行异常处理，不静默放行。
- 写入失败：**不得静默放行**。在对话里报告错误，`.skill-result.json` 的 `conclusion` 写「失败」，
  并以非零退出码结束；不要改写结论为「通过」。

评论固定结构（由脚本生成，不要手写替代）。

通过：

```text
h3. (/) G1-Pre 通过
=====需求评估摘要（by G1-PRE agent）=====

h4. 1. 需求背景
客户在语音 IVR 场景使用 200 路并发，当前部署无法满足扩容需要。

h4. 2. 需求目标
* 评估 200 路并发的资源配置
* 给出可执行的部署建议

h4. 3. 需求要点
* 现有部署为单机双卡
* 客户希望本月完成容量评估

h4. 4. 建议澄清方向
* 需要确认：是否需要同时给出压测方案
  原因：客户只提出资源评估，压测范围尚未说明

_由 G1-Pre 基于本次原始材料生成，用于需求接手前快速理解，不构成产品、技术或验收方案。_
```

不通过：

```text
h3. (!) G1-Pre 不通过（2项）
*报告人：* [~zhangsan]
*本次阻断项：*
# 期望结果不可验证
# 核心迁移附件缺失

h4. 1. 目标不可验证
*当前问题：* 描述只写希望导出更快，未说明期望达到什么结果。
*依据：* description:paragraph-2
*最小补充动作：* 请补充期望的导出结果，例如时限、条数或可用性要求。

h4. 2. 必需证据缺失
*当前问题：* 正文声称提供迁移清单，附件区只有空模板。
*依据：* attachment:10002
*最小补充动作：* 请上传包含实际迁移对象的清单。
```

评论纪律：

- 使用 Jira Wiki Markup；标题固定为 `h3. (/) G1-Pre 通过` 或 `h3. (!) G1-Pre 不通过（N项）`，
  `N` 只统计阻断问题，不包含建议。
- 通过评论固定保留「需求评估摘要」分隔行、四个 `h4` 章节和末尾免责声明；建议澄清方向为空时写「无」。
- 需求评估摘要只能总结原始材料，不得新增规则、字段、接口、方案或验收标准，也不输出建议接手角色。
- 不通过评论先列 `*本次阻断项：*` 摘要列表，再逐项用 `h4. 序号. 问题分类` 展开。
- 每条阻断固定使用 `*当前问题：*`、`*依据：*`、`*最小补充动作：*` 三个加粗标签。
- 一次列出全部独立阻断项，不只写第一条。
- 不通过必须 `@报告人`，且必须使用 Jira 用户 key（如 `[~zhangsan]`），不得用显示名或邮箱拼接；
  报告人缺失时在对话里说明，不得伪造提及。
- 评论中不得出现规则版本、内容指纹、sha256 或「G1-Pre 指纹」。

### 9. 写机器可读结果并清理

在任务根目录（当前工作目录）写入 `.skill-result.json`：

```json
{
  "schemaVersion": 1,
  "skillId": "jira-gate-1-pre",
  "issueKey": "KEY-123",
  "conclusion": "通过",
  "summary": "三项检查全部通过（technical_assessment）；评论 ID 12345",
  "blockingCount": 0
}
```

结论取值：

- 判定通过且评论已写入或幂等跳过：`通过`。
- 判定不通过且评论已写入或幂等跳过：`不通过`。
- `dry_run=true` 时结论仍按业务判定写 `通过` / `不通过`，在 `summary` 中注明「未写入 Jira」。
- `applicability=not_applicable`（类型不在适用范围内）：`不适用`。
- 核心附件不可读等执行异常：`无法判定`；Jira 读写失败：`失败`。**不要伪造通过**。
- `blockingCount` 为 `findings` 的条数。

清理中间产物：

```bash
node <技能目录>/scripts/workdir-cleanup.mjs --key <KEY>
```

只有需要保留现场排查时才跳过清理；清理失败只记告警，不改变业务结论。

最后用简洁中文总结：单据类型、需求类型、结论、关键阻断项（或「无阻断项」）、
评论 ID / 是否幂等跳过、是否 dry_run，以及未完成的部分与原因。

---

## 异常处理

| 情况 | 处理 |
|---|---|
| 单据类型不在五类内，或目标状态是 `评审中` 的内部流转 | 立即跳过；`applicability=not_applicable`，不写评论，`.skill-result.json` 写「不适用」。 |
| 核心附件下载或解析失败（正文依赖） | 记 `executionStatus=execution_error`，不写通过/不通过评论，`.skill-result.json` 写「无法判定」。 |
| 补充附件不可读 | 只写建议，不改变结论。 |
| 正文声称提供材料但实际缺失或只有空模板 | 不通过，`required_evidence_missing`。 |
| Jira 读取失败 | 报错退出，`.skill-result.json` 写「失败」，不要猜测结论。 |
| Jira 写入评论失败 | 报错退出，结论写「失败」，不要静默放行。 |
| 最新评论正文与本轮一致 | 不重复评论，沿用历史评论 ID；结论仍按本轮判定返回。 |
| 报告人缺失 | 照常写入不通过评论，但在对话里说明无法 `@`；不得伪造提及。 |
| `dry_run=true` | 只返回拟写评论，不写评论、不改字段、不流转。 |

## 禁止事项

- 不要流转状态、修改字段、上传或删除附件。
- 不要判断或输出必须由哪个角色接手。
- 不要因为需求是非功能、效果、技术评估或项目任务就绕过门禁。
- 不要把「方案未定」「待决策」当成需求不合格。
- 不要要求提交人回答编解码、模型路径、资源维度、接口字段等下游技术答案。
- 不要因为产品归属、截止日期、问题发现产品缺失而阻断。
- 不要因为问题类型不匹配单独阻断；只给建议，按正文实际缺口判断。
- 不要凭标题通过，也不要因描述短就直接拒绝。
- 不要要求上游提前提供完整 PRD、技术方案、测试方案或验收标准。
- 不要声称读取了不可读附件或编造证据。
- 不要按 Issue Key、经办人或项目名称设置白名单。
- 不要在 Jira 评论中显示规则版本、内容指纹、sha256 或「G1-Pre 指纹」。
- 不要把 `jira-gate-1-pre` 与 `jira-gate-1` 混用：G1-Pre 看原始诉求是否清楚，
  G1 看 PRD 是否达到需求准出标准。

## 验收清单

- 五种问题类型（客户用户故事、故事、项目概算评估、任务、项目实施阶段）分别跑通通过/不通过各一次。
- 六种需求类型（功能/效果/技术评估/迁移压测/研究文档/项目任务）均按同一门禁目标判定，
  只切换观察重点。
- `故事` 在 `待办 → 需求澄清` 前具备场景、现状、问题和期望时通过；仅有题目或需求标题时不通过。
- `客户用户故事` / `故事` 显式传入目标状态 `评审中` 时跳过，不写评论。
- 只有标题或「详见附件」时必须不通过，并归 `unstructured_description`。
- 正文提到截图/日志但材料缺失时必须不通过，并归 `required_evidence_missing`。
- 仅缺产品归属/截止日期时必须通过，并写入建议澄清方向。
- 通过评论包含 `=====需求评估摘要（by G1-PRE agent）=====`、四个 `h4` 与免责声明。
- 不通过评论包含 `*本次阻断项：*` 摘要列表与 `@报告人`，每条阻断都有当前问题、依据和最小补充动作。
- 核心附件不可读时按执行异常处理，不写通过/不通过评论。
- 同一内容重复运行、最新评论正文一致时不新增评论。
- 修改标题或描述后重新运行会写新评论。
- 所有评论均不出现规则版本、内容指纹、sha256 或「G1-Pre 指纹」。
- `dry_run=true` 时 Jira 上没有任何新增评论。
