---
name: jira-gate-1-pre
description: Jira 上游需求 G1-Pre 准入检查。当用户给出 Jira 链接或单号并要求"检查原始诉求是否清楚 / 能不能进入需求澄清或处理中 / 做 G1-Pre 预检"时使用。只处理类型为「客户用户故事」「项目概算评估」「任务」「项目实施阶段」的单子，按 F01~F07 七项事实逐项判定，只输出「通过 / 不通过」；不通过时按固定分类列出补充要求并在评论中 @ 报告人。本技能只读单据、只写 G1-Pre 评论（同一内容指纹不重复），不流转状态、不修改字段、不上传或删除附件。
metadata:
  short-description: Jira 上游需求 G1-Pre 准入检查
---

# Jira 上游需求 G1-Pre 准入检查

用户给一个 Jira 链接，你读取标题、描述、字段、附件和必要评论，判断**原始诉求是否足以让产品、研发或算法接手**，给出 `通过` / `不通过` 结论，并把结论写入这张单子的评论。

## 交付物

本技能只有一个写入型交付物：**G1-Pre 评论**（同一内容指纹只写一条，内容变化后才写新评论）。

1. Jira 评论：通过时只写结论与可选规范建议；不通过时按固定分类列出全部阻断项，并在「请补充后重检」处 `@报告人`。
2. 对话摘要：单据类型 + 结论 + 关键阻断项 + 评论 ID / 是否因幂等跳过 + 未完成原因。
3. 机器可读结果：任务根目录 `.skill-result.json`，用于平台统计。

**本技能不生成正式报告文档，不上传附件，不删除附件，不流转状态，不修改任何 Jira 字段。** 本地只允许在 `work/jira-gate/<KEY>/` 下存放附件与解析中间文件，交付完成后清理。

## 与规则文档的落地差异

规则文档假设存在一个 Jira 接入层在 transition 前调用本技能，并由接入层决定是否放行。本仓库没有该拦截层，因此按以下方式落地：

- **本技能不做 transition**：只输出结论与评论，调用方拿到 `conclusion` 后自行决定是否继续原 transition。
- **评论由本技能写入**：与 `jira-gate-1` 的约定一致；`dry_run=true` 时只返回拟写评论，不写 Jira。
- **幂等键落在评论里**：每条 G1-Pre 评论末尾固定带一行 `G1-Pre 指纹：V1.0 / sha256:...`，同一单号 + 指纹 + 版本已存在时不重复评论。
- **接入层未传 transition 时**：按问题类型推断预期目标状态并记录 `transition.provided=false`，该值只用于内容指纹，绝不执行流转。
- **类型过滤**：只认 Jira 的 `issuetype`，四种类型之外直接跳过；不要用标题、标签或描述猜测类型。

## 输入

必需输入：

- `jira_url`：Jira 链接或单号。

可选输入（来自任务提示词的 `{inputs_lines}`；缺失时按默认值处理）：

- `transition_id`：接入层实际触发的 transition ID。
- `source_status`：触发前状态；默认取单据当前状态。
- `target_status`：允许继续的目标状态；默认按问题类型推断。
- `dry_run`：`true` 时只判定并生成评论，不写 Jira。

四种问题类型的预期 transition：

| 问题类型 | 动作 | 目标状态 |
|---|---|---|
| 客户用户故事 | 跟进需求 | 需求澄清 |
| 项目概算评估 | 开始评估 | 处理中 |
| 任务 | 开始进行 | 处理中 |
| 项目实施阶段 | 开始评估 | 处理中 |

## 判定依据

判定标准以 [references/g1-pre-rules.md](references/g1-pre-rules.md) 为准，**先读它再开始判定**。核心口径：

取数、附件解析与写评论的接口细节见 [references/jira-api.md](references/jira-api.md)。

- `F01`~`F07` 必须同时满足：对象、场景、当前状态、问题或影响、期望结果、内部一致性、可供执行。
- 不得因未使用模板、章节名称不同或没有 BadCase 而退回。
- 不得仅凭标题或一句口号通过；描述至少要能分别组织出「场景或现状」「问题或影响」「期望结果」。
- 产品归属、截止日期、问题发现产品、业务价值、优先级、Story Points、技术方案等字段缺失**不阻断**，只给规范建议。
- 多诉求逐项判定；只阻断缺失项，不因数量多退回。
- 每条阻断问题只归一个主分类，分类枚举见规则文档第 5 节。

---

## 执行流程

### 0. 类型预检（必须最先执行）

```bash
node <技能目录>/scripts/g1-pre.mjs precheck "<JIRA_URL>" \
  [--transition-id "<ID>"] [--source-status "<源状态>"] [--target-status "<目标状态>"]
```

- `supported=true`：继续第 1 步。
- `supported=false`：立即停止。不读附件、不判定、不写评论、不写 `.skill-result.json` 中的「通过」，只在对话里说明「该单类型为 X，不属于 G1-Pre 适用的四类上游事项，按规则跳过」。
- 若单据类型属于四类之一，但明显是会议、出差、考勤、运维、权限申请等事务性记录，按规则文档 §2.2 由接入层过滤；本技能仍按正文实际内容判定，不凭标题关键词自行豁免。

### 1. 读取单据上下文

```bash
node <技能目录>/scripts/g1-pre.mjs context "<JIRA_URL>" --description-limit 20000 --comments 10 \
  [--transition-id "<ID>"] [--source-status "<源状态>"] [--target-status "<目标状态>"]
```

输出包含标题、完整描述、类型、状态、报告人及 `[~name]` 写法、自定义字段、附件清单、最近评论与 transition 上下文。

取数要点：

- 先以标题和描述建立原始诉求；字段用于补充对象与客户；附件用于补充过程、样例、日志和指标。
- 评论只能补充事实，**不能替代上游原始诉求**。
- 如果描述只有「详见附件」「看截图」「看日志」「与某人沟通后处理」，即使附件完整，也判 `不通过` / `missing_key_information`。

### 2. 下载并解析附件

仅有附件时才需要执行；没有附件直接进入第 3 步。

```bash
node <技能目录>/scripts/jira-cli.mjs attachments "<JIRA_URL>" "work/jira-gate/<KEY>/attachments"
python <技能目录>/scripts/extract.py "work/jira-gate/<KEY>/attachments" --out "work/jira-gate/<KEY>/attachments.txt"
```

- 附件下载失败、压缩包解压失败或文档解析失败时，如实记为「附件不可读」；**不得当成「附件没有内容」，不得臆测包内内容，不得声称已核验**。
- 正文明确提到截图、日志或其他附件，而对应材料缺失或不可读时，判 `不通过` / `evidence_inconsistent_or_missing`。
- 图片附件用图像查看能力查看；无法查看时如实说明，不猜内容。
- 引用证据要写到附件内部路径，例如 `附件.zip/PRD.md §4.2`。

### 3. 建立诉求清单

从标题和描述中拆出全部独立诉求，每个诉求记录：

```text
claim_id / 对象 / 场景 / 当前状态 / 问题或影响 / 期望结果 / 证据引用
```

一个句子包含多个独立目标时拆成多个 claim，不要只看第一项或最后一项。只有一个诉求时也按同一结构记录。

### 4. 逐项判定

对每个诉求检查 `F01`~`F07`，再按问题类型套用规则文档 §4 的条件场景：

- 客户用户故事：功能/Bug/优化看「场景 + 当前结果 + 期望」；性能看「受影响业务 + 现象或基线 + 可核验目标」；AI/LLM 看「业务任务 + 代表性输入或失败现象 + 当前输出问题 + 期望效果」。
- 项目概算评估：看「评估对象或范围 + 为什么需要评估 + 期望结果（可行性/工作量/成本/周期/改造影响至少一项）」。
- 任务：看「要完成什么 + 为什么 + 可识别的完成结果或交付物」。
- 项目实施阶段：看「阶段目标或工作 + 当前阶段状态 + 阶段交付物或完成结果」。

判定纪律：

- 证据不足、语义冲突或无法唯一判断，一律按不满足处理。
- 不把「类型不匹配」「字段缺失」「模板格式」列为阻断分类；类型不匹配只给规范建议。
- 同一根因只列一条 finding；不同诉求的独立缺口分别保留。
- `checks` 必须覆盖 F01~F07，每条写真实证据，不写推理过程。

### 5. 写判定结果文件

在 `work/jira-gate/<KEY>/result.json` 写下结构化结果。字段名使用本技能约定的驼峰形式，`g1-pre.mjs comment` 会据此渲染评论：

```json
{
  "issueKey": "KEY-123",
  "issueType": "客户用户故事",
  "conclusion": "fail",
  "ruleVersion": "V1.0",
  "contentHash": "sha256:...",
  "reporterMention": "[~zhangsan]",
  "checks": [
    { "id": "F01", "status": "pass", "evidence": "标题明确客户与页面" },
    { "id": "F02", "status": "pass", "evidence": "描述写明在订单导出场景操作" },
    { "id": "F03", "status": "pass", "evidence": "描述写明当前导出报错" },
    { "id": "F04", "status": "fail", "evidence": "未说明报错对业务的影响" },
    { "id": "F05", "status": "pass", "evidence": "希望导出成功并保留筛选条件" },
    { "id": "F06", "status": "pass", "evidence": "标题、描述与截图一致" },
    { "id": "F07", "status": "fail", "evidence": "接手人无法判断修复范围" }
  ],
  "findings": [
    {
      "category": "missing_key_information",
      "currentProblem": "描述仅说明导出报错，未说明影响哪些角色或业务",
      "suggestedSupplement": "请补充报错影响的使用角色、发生频率和业务后果"
    }
  ],
  "advisoryFindings": ["建议确认问题类型是否与实际诉求一致"],
  "fieldHints": ["建议补充产品归属"]
}
```

写入前自检：

- `conclusion` 只有 `pass` / `fail`。
- `contentHash` 必须是第 6 步算出的完整 `sha256:...`；不要手填占位值。
- `findings` 为空必须是 `pass`；非空必须是 `fail`。
- 每条 finding 都有合法的 `category`、具体的 `currentProblem` 和最小补充要求 `suggestedSupplement`。
- `checks` 覆盖 F01~F07。

### 6. 计算内容指纹并判重

附件已下载时**必须带 `--dir`**，让指纹覆盖附件的 sha256；否则指纹只覆盖附件元数据。

```bash
node <技能目录>/scripts/g1-pre.mjs hash "<JIRA_URL>" --dir "work/jira-gate/<KEY>/attachments" \
  [--transition-id "<ID>"] [--source-status "<源状态>"] [--target-status "<目标状态>"]
```

把输出里的 `contentHash` 写回 `result.json` 的 `contentHash`，然后判重：

```bash
node <技能目录>/scripts/g1-pre.mjs history "<JIRA_URL>" --hash "<contentHash>" --version V1.0
```

- `duplicate=true`：同一单号 + 指纹 + 规则版本已处理过。**不重复写评论**，在对话里说明「内容未变化，沿用评论 <ID>」，直接进入第 8 步。
- `duplicate=false`：继续第 7 步。
- 标题、描述、附件内容或参与判定的字段变化后，指纹会变化，应重新判定并写新评论。

### 7. 渲染并写入评论

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
- 写入失败：**不得静默放行**。在对话里报告错误，`.skill-result.json` 的 `conclusion` 写「失败」，并以非零退出码结束；不要改写结论为「通过」。

评论固定结构（由脚本生成，不要手写替代）：

通过：

```text
G1-Pre 结论：通过

规范建议：
- 建议补充产品归属

G1-Pre 指纹：V1.0 / sha256:...
```

不通过：

```text
G1-Pre 结论：不通过

[~reporter] 请补充后重检：
1、缺失关键信息：
当前问题：...
建议调整：...

2、描述与证据不一致或缺失：
当前问题：...
建议调整：...

规范建议：
- ...

G1-Pre 指纹：V1.0 / sha256:...
```

评论纪律：

- 不写「已确认的信息」章节，不展示内部推理或完整检查表。
- 每条阻断固定使用 `分类 / 当前问题 / 建议调整` 三段。
- 一次列出全部独立阻断项，不只写第一条。
- `规范建议` 只在确有建议时出现；没有建议就不写「无建议」。
- 不通过必须 `@报告人`；报告人缺失时在对话里说明，不得伪造提及。

### 8. 写机器可读结果并清理

在任务根目录（当前工作目录）写入 `.skill-result.json`：

```json
{
  "schemaVersion": 1,
  "skillId": "jira-gate-1-pre",
  "issueKey": "KEY-123",
  "conclusion": "通过",
  "summary": "F01~F07 全部满足；评论 ID 12345",
  "blockingCount": 0
}
```

结论取值：

- 判定通过且评论已写入或幂等跳过：`通过`。
- 判定不通过且评论已写入或幂等跳过：`不通过`。
- `dry_run=true` 时结论仍按业务判定写 `通过` / `不通过`，在 `summary` 中注明「未写入 Jira」。
- 类型不在适用范围内：`不适用`。
- 附件不可读、Jira 写入失败等运营性异常：`失败` 或 `无法判定`，**不要伪造通过**。
- `blockingCount` 为 `findings` 的条数。

清理中间产物：

```bash
node <技能目录>/scripts/workdir-cleanup.mjs --key <KEY>
```

只有需要保留现场排查时才跳过清理；清理失败只记告警，不改变业务结论。

最后用简洁中文总结：单据类型、结论、关键阻断项（或「无阻断项」）、评论 ID / 是否幂等跳过、是否 dry_run，以及未完成的部分与原因。

---

## 异常处理

| 情况 | 处理 |
|---|---|
| 单据类型不在四类内 | 立即跳过，不写评论、不写统计结论为通过；`.skill-result.json` 写「不适用」。 |
| 附件下载失败 | 记「附件不可读」；若正文依赖该附件才可判断，结论为不通过 / `evidence_inconsistent_or_missing`；否则在证据里如实说明。 |
| 附件解析失败 | 同上，不能当成附件为空。 |
| Jira 读取失败 | 报错退出，`.skill-result.json` 写「失败」，不要猜测结论。 |
| Jira 写入评论失败 | 报错退出，结论写「失败」，不要静默放行。 |
| 内容指纹相同 | 不重复评论，沿用历史评论 ID，结论按历史结论返回。 |
| `dry_run=true` | 只返回拟写评论，不写评论、不改字段、不流转。 |

## 禁止事项

- 不要流转状态、修改字段、上传或删除附件。
- 不要因为产品归属、截止日期、问题发现产品缺失而阻断。
- 不要因为问题类型不匹配单独阻断；只给规范建议，按正文实际缺口判断。
- 不要凭标题通过，也不要因描述短就直接拒绝。
- 不要要求上游提前提供完整 PRD、技术方案、测试方案或验收标准。
- 不要声称读取了不可读附件或编造证据。
- 不要按 Issue Key、经办人或项目名称设置白名单。
- 不要把 `jira-gate-1-pre` 与 `jira-gate-1` 混用：G1-Pre 看原始诉求是否清楚，G1 看 PRD 是否达到需求准出标准。

## 验收清单

- 四种问题类型分别跑通通过/不通过各一次。
- 只有标题或「详见附件」时必须不通过，并归 `missing_key_information`。
- 正文提到截图/日志但材料缺失时必须不通过，并归 `evidence_inconsistent_or_missing`。
- 仅缺产品归属/截止日期时必须通过，并出现规范建议。
- 同一内容重复运行时不新增评论。
- 修改标题或描述后重新运行会写新评论。
- 不通过评论包含 `@报告人`，且每条阻断都有当前问题和建议调整。
- `dry_run=true` 时 Jira 上没有任何新增评论。
