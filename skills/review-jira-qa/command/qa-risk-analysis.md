---
description: 阶段一需求准入门禁：读取 Jira 最新需求附件，生成需求准入门禁清单 HTML，上传附件并同步评论
argument-hint: <Jira需求Key或URL>
allowed-tools: Read, Write, Edit, Bash(node:*), Glob, Grep
---

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

1. 前置检查：

```bash
node "${CLAUDE_SKILL_DIR}/bin/preflight.mjs" --key <KEY> --stage 1
```

2. 拉取 issue 与附件：

```bash
node "${CLAUDE_SKILL_DIR}/bin/jira-read.mjs" --key <KEY>
node "${CLAUDE_SKILL_DIR}/bin/jira-download.mjs" --key <KEY> --run-id <RUN_ID>
node "${CLAUDE_SKILL_DIR}/bin/materials.mjs" --run-id <RUN_ID>
```

记下 `RUN_ID`、`SCOPE`、`REQUIREMENT_NAME`、`PRIMARY_ID`。

如果 `jira-download` 返回的 `QA_RESULT_JSON` 中 `status=SKIPPED` / `applicable=false`：

- 说明该单不符合门禁输入要求（无附件、无需求侧材料、主材料不唯一等），**不适用需求准入门禁**；
- 向用户说明 `reason` 和 `errorCode`，然后停止；
- 不生成 HTML、不上传附件、不写 Jira 评论、不进入阶段二。

3. 读 `${CLAUDE_SKILL_DIR}/prompts/stage1-gate.md` 与 `.qa/runs/<RUN_ID>/materials-digest.md`，逐份核读需求侧材料：
   - 主 PRD、Overview、原型、页面材料、导入导出模板；
   - 如果材料里出现 `<KEY>-需求描述.md`（来源 Jira 需求字段），把它作为主需求材料正常评审；只有 PRD、没有 HTML 也正常评审；
   - JS 驱动 HTML 的脚本内正文已在摘要中；PDF/图片用 `Read` 直接打开；
   - 抽取为空且无法查看的材料记为「材料不可读」，不得判通过。

4. 写 `.qa/runs/<RUN_ID>/gate.spec.json`（只写 JSON，不写 HTML）。字段见 stage1-gate.md。

5. 渲染并校验：

```bash
node "${CLAUDE_SKILL_DIR}/bin/render-gate.mjs" --spec ".qa/runs/<RUN_ID>/gate.spec.json" --version <版本>
node "${CLAUDE_SKILL_DIR}/bin/validate-gate.mjs" --html <HTML> --spec ".qa/runs/<RUN_ID>/gate.spec.json"
```

校验失败：按错误修正 spec 后重试，最多两次；仍失败则停止，不上传。

6. 上传附件并同步评论：

```bash
node "${CLAUDE_SKILL_DIR}/bin/jira-upload.mjs" --key <KEY> --file <HTML> --run-id "<RUN_ID>" --stage risk-analysis
node "${CLAUDE_SKILL_DIR}/bin/jira-comment.mjs" --key <KEY> --body <COMMENT> --run-id "<RUN_ID>" --stage risk-analysis
```

7. 输出运行结果：

```bash
node "${CLAUDE_SKILL_DIR}/bin/result.mjs" --stage risk-analysis --key <KEY> --run-id "<RUN_ID>" --spec ".qa/runs/<RUN_ID>/gate.spec.json"
```

## 约束

- 结论只有「通过 / 暂不通过」；不通过项编号 1..N 连续，类型只有「材料冲突 / 材料未明确」。
- 每条不通过项必须有材料原文依据；不写具体改法。
- 结论=暂不通过时仍正常结束（exit 0），供 Jira 规则据 `QA_RESULT_JSON.conclusion` 决定是否退回「评审中」。
- **阶段一结束即停止**，不执行 `/qa-design`；通过时只提示下一步命令。
- 不修改需求描述、不改状态、不删除产品材料；旧版本只删除同交付物族。
