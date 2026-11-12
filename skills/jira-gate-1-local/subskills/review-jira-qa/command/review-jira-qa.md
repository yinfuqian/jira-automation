---
description: 阶段一需求准入门禁：读取 Jira 最新需求附件，按 B1-B7 / C1-C6 判定，生成需求准入门禁清单 HTML，上传附件并同步 QA 评论
argument-hint: <Jira需求Key或URL>
allowed-tools: Read, Write, Edit, Bash(node:*), Glob, Grep
---

执行 QA 阶段一：需求准入门禁。技能目录用 `${CLAUDE_SKILL_DIR}`，工作区用
`${QA_WORKSPACE}`（缺省当前目录）。本命令不执行阶段二/三。

执行前把 `${CLAUDE_SKILL_DIR}` 替换为技能目录绝对路径；`${QA_WORKSPACE:-$PWD}` 由 shell 展开。
逐条执行下述命令，不要用 `&&` 串联。

## 输入

```
$ARGUMENTS
```

取第一个非选项参数作为 Jira Issue Key 或 `/browse/<KEY>` URL。为空时向用户索要，不要猜。

可选 `--version x.y` 指定本次交付物版本；未指定时使用 `prepare.mjs` 输出的
`suggestedVersion`（按 Jira 已有门禁附件版本递增）。

## 执行

### 1. 准备材料、复用判定与 AC 覆盖基线

```bash
node "${CLAUDE_SKILL_DIR}/bin/prepare.mjs" --key <KEY> --stage 1
```

读输出 JSON 的 `status`：

- `READY`：继续。记下 `runId`、`scope`、`requirementName`、`primaryId`、`digest`、
  `coverageRequired`、`requiredAcs`、`suggestedVersion`。
- `REUSED`：材料附件 ID 集合与上一版门禁完全一致，直接复用。向调用方报告 `conclusion`、
  `suggestedVersion` 与 `commentId`，**不要重新分析、不要新建附件或评论、不要进入后续步骤**。
- `SKIPPED`：输入不适用门禁，已写「评审不适用」评论。向调用方说明 `reason` 与 `errorCode`
  后停止，不生成 HTML、不上传附件。
- `status=ERROR` / `ok=false`：停止本技能剩余步骤，把错误原因交还调用方；不要等待人工确认。

`prepare` 内部已完成 preflight、jira-read、jira-download、reuse、materials、coverage，
不要重复执行这些脚本。只有排障时才单独运行。

### 2. 核读材料并生成扫描候选

读 `${CLAUDE_SKILL_DIR}/prompts/stage1-gate.md` 与 `prepare` 输出的 `digest`
（`materials-digest.md`），逐份核读需求侧材料：

- 主 PRD、Overview、原型、页面材料、导入导出模板；
- 以 PRD 为唯一需求事实源；Overview/Prototype 是 PRD 的子集，允许不完整但不得与 PRD 冲突；
- `<KEY>-需求描述.md`（来源 Jira 需求字段）可作为主材料；只有 PRD、没有 HTML 也正常评审；
- PDF/图片用 `Read` 直接打开；抽取为空且无法查看的材料记为「材料不可读」，不得判通过。

对 `materials.json` 中全部 active 的需求侧材料做结构扫描：

```bash
node "${CLAUDE_SKILL_DIR}/bin/scan.mjs" --source <材料文件1> --source <材料文件2> ... \
  --out ".qa/runs/<RUN_ID>/candidates.json"
```

材料文件优先取 `materials.json` 的 `extractedPath`，否则取 `localFile`。

### 3. 写 gate.spec.json

只写 `.qa/runs/<RUN_ID>/gate.spec.json`，字段见 `stage1-gate.md`：

- `conclusion` 只能是「通过 / 暂不通过」；通过时 `failures` 为空，暂不通过时至少 1 项；
- `checks` 必须覆盖 B1-B7、C1-C6 共 13 项；
- `coverage` 必须覆盖 `coverage-required.json` 的全部 AC，每条 `checked=true`；
- 每条 failure 带 `facts[].quote` 与 `criteria`；`origin=本版材料变更引入` 时必须带
  `introducedBy.quote`；
- `scans` 必须逐条回应 `candidates.json` 的 `decision` 与 `reason`；
- `materials` 必须与 `materials.json` 的附件 ID、文件名、上传时间一致。

### 4. 渲染并校验

```bash
node "${CLAUDE_SKILL_DIR}/bin/render-gate.mjs" --spec ".qa/runs/<RUN_ID>/gate.spec.json" \
  --version <版本>
node "${CLAUDE_SKILL_DIR}/bin/validate-gate.mjs" --html <HTML> \
  --spec ".qa/runs/<RUN_ID>/gate.spec.json" \
  --coverage-required ".qa/runs/<RUN_ID>/coverage-required.json" \
  --candidates ".qa/runs/<RUN_ID>/candidates.json" \
  --source <材料文件1> --source <材料文件2> ...
```

校验失败：按错误修正 spec 后重试，最多两次；仍失败则停止，不上传。

### 5. 上传附件、同步评论并输出结果

`render-gate.mjs` 会输出 `HTML=` 与 `COMMENT=`，直接使用：

```bash
node "${CLAUDE_SKILL_DIR}/bin/jira-upload.mjs" --key <KEY> --file <HTML> \
  --run-id "<RUN_ID>" --stage risk-analysis
node "${CLAUDE_SKILL_DIR}/bin/jira-comment.mjs" --key <KEY> --body <COMMENT> \
  --run-id "<RUN_ID>" --stage risk-analysis
node "${CLAUDE_SKILL_DIR}/bin/result.mjs" --stage risk-analysis --key <KEY> \
  --run-id "<RUN_ID>" --spec ".qa/runs/<RUN_ID>/gate.spec.json"
```

## 约束

- 只执行阶段一，不执行 `/qa-design`，不生成测试方案、XMind、Excel 或 Xray CSV。
- 结论只有「通过 / 暂不通过」；不通过项编号 1..N 连续，类型只有「材料冲突 / 材料未明确」。
- 每条不通过项必须有材料原文依据；不写具体改法。
- 结论=暂不通过时仍正常结束（exit 0），供 Jira 规则据 `QA_RESULT_JSON.conclusion`
  决定是否退回「评审中」；G1 调用时父技能会据「暂不通过」把最终结论置为「退回补充」。
- 所有脚本统一 exit 0；`status=ERROR` / `ok=false` 记为「未完成」，`status=SKIPPED` 记为
  「不适用」，两种情况都不得阻塞父流程。
- 不修改需求描述、不改状态、不删除产品材料；旧版本只删除同交付物族。
