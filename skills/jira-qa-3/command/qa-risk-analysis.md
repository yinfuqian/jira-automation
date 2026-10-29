---
description: 阶段一需求准入门禁：读取 Jira 最新需求附件，生成需求准入门禁清单 HTML，上传附件并同步评论
argument-hint: <Jira需求Key或URL>
allowed-tools: Read, Write, Edit, Bash(node:*), Glob, Grep
---

> 本文件仅用于 `preflight.mjs` 安装包完整性校验。`jira-qa` 技能不执行阶段一，禁止调用本命令；
> 阶段一请使用 `review-jira-qa`。

执行 QA 阶段一：需求准入门禁。技能目录用 `${CLAUDE_SKILL_DIR}`，工作区用 `${QA_WORKSPACE}`（缺省当前目录）。

执行前把 `${CLAUDE_SKILL_DIR}` 替换为技能目录绝对路径；`${QA_WORKSPACE:-$PWD}` 由 shell 展开。

逐条执行下述命令，不要用 `&&` 串联；任一脚本输出 `status=ERROR` 或 `status=SKIPPED` 时立即停止，不要继续后续步骤。

## 输入

```
$ARGUMENTS
```

取第一个非选项参数作为 Jira Issue Key 或 `/browse/<KEY>` URL。为空时向用户索要，不要猜。

可选 `--version x.y` 指定本次交付物版本；未指定时使用 `materials.json` 的 `suggestedDeliverableVersion`（按 Jira 已有门禁附件版本递增）。

## 执行

1. 一次完成前置检查、材料刷新、复用判定、材料摘要与 AC 提取：

```bash
node "${CLAUDE_SKILL_DIR}/bin/prepare.mjs" --key <KEY> --stage 1
```

读输出 JSON 的 `status`：

- `REUSED`：材料附件 ID 集合与上一版门禁完全一致，直接复用。向调用方报告 `conclusion` 与 `suggestedVersion`，**不要重新分析、不要新建附件或评论、不要进入后续步骤**。
- `SKIPPED`：输入不适用门禁，已写「评审不适用」评论。向调用方说明 `reason` 与 `errorCode` 后停止，不生成 HTML、不上传附件、不进入阶段二。
- `READY`：继续。记下 `runId`、`scope`、`requirementName`、`primaryId`、`digest`、`coverageRequired`、`requiredAcs`、`suggestedVersion`。

`prepare` 内部已包含 preflight、jira-read、jira-download、reuse、materials、coverage 六步，任一步失败会整体返回 `status=ERROR`，不需要单独再跑这些脚本。需要强制重审时加 `--force`；单个步骤排障时再单独执行对应脚本。

2. 读 `${CLAUDE_SKILL_DIR}/prompts/stage1-gate.md` 与 `digest` 指向的 `materials-digest.md`，逐份核读需求侧材料：
   - 主 PRD、Overview、原型、页面材料、导入导出模板；
   - 以 PRD 为唯一需求事实源；Overview/Prototype 是 PRD 的子集，允许不完整但不得与 PRD 冲突；子集缺失不单独计为冲突，与 PRD 相反或范围不同计为材料冲突；
   - 如果材料里出现 `<KEY>-需求描述.md`（来源 Jira 需求字段），把它作为主需求材料正常评审；只有 PRD、没有 HTML 也正常评审；
   - JS 驱动 HTML 的脚本内正文已在摘要中；PDF/图片用 `Read` 直接打开；
   - 抽取为空且无法查看的材料记为「材料不可读」，不得判通过。

3. 先生成结构扫描候选，再写 `.qa/runs/<RUN_ID>/gate.spec.json`（只写 JSON，不写 HTML）。字段见 stage1-gate.md。

```bash
node "${CLAUDE_SKILL_DIR}/bin/scan.mjs" --source <材料文件1> --source <材料文件2> ... --out ".qa/runs/<RUN_ID>/candidates.json"
```

   - `checks` 必须覆盖 B1-B7、C1-C6 共 13 项；`scans` 必须逐条回应 `candidates.json`。
   - 必须按 `coverage-required.json` 填满 `coverage`，每个 AC 一条，`checked=true`。
   - 每条 failure 必须带 `facts[].quote` 与 `criteria`；有上一版门禁评论时，按「复评稳定性」区分 `本版材料变更引入` / `上一版漏检`，不得把上一版漏检写成新材料问题。

4. 渲染并校验：

```bash
node "${CLAUDE_SKILL_DIR}/bin/render-gate.mjs" --spec ".qa/runs/<RUN_ID>/gate.spec.json" --version <版本>
node "${CLAUDE_SKILL_DIR}/bin/validate-gate.mjs" --html <HTML> --spec ".qa/runs/<RUN_ID>/gate.spec.json" --coverage-required ".qa/runs/<RUN_ID>/coverage-required.json" --candidates ".qa/runs/<RUN_ID>/candidates.json" \
  --source <材料文件1> --source <材料文件2> ...
```

校验失败：按错误修正 spec 后重试，最多两次；仍失败则停止，不上传。

5. 上传附件并同步评论：

```bash
node "${CLAUDE_SKILL_DIR}/bin/jira-upload.mjs" --key <KEY> --file <HTML> --run-id "<RUN_ID>" --stage risk-analysis
node "${CLAUDE_SKILL_DIR}/bin/jira-comment.mjs" --key <KEY> --body <COMMENT> --run-id "<RUN_ID>" --stage risk-analysis
```

6. 输出运行结果：

```bash
node "${CLAUDE_SKILL_DIR}/bin/result.mjs" --stage risk-analysis --key <KEY> --run-id "<RUN_ID>" --spec ".qa/runs/<RUN_ID>/gate.spec.json"
```

## 约束

- 结论只有「通过 / 暂不通过」；不通过项编号 1..N 连续，类型只有「材料冲突 / 材料未明确」。
- 每条不通过项必须有材料原文依据；不写具体改法。
- 结论=暂不通过时仍正常结束（exit 0），供 Jira 规则据 `QA_RESULT_JSON.conclusion` 决定是否退回「评审中」。
- **阶段一结束即停止**，不执行 `/qa-design`；通过时只提示下一步命令。
- 不修改需求描述、不改状态、不删除产品材料；旧版本只删除同交付物族。
