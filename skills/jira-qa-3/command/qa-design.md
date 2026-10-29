---
description: 阶段二测试方案与测试用例 XMind：门禁只读校验后生成方案与 XMind，上传并停止等待人工复核
argument-hint: <Jira需求Key或URL>
allowed-tools: Read, Write, Edit, Bash(node:*), Glob, Grep
---

执行 QA 阶段二。技能目录用 `${CLAUDE_SKILL_DIR}`，工作区用 `${QA_WORKSPACE}`（缺省当前目录）。

执行前把 `${CLAUDE_SKILL_DIR}` 替换为技能目录绝对路径；`${QA_WORKSPACE:-$PWD}` 由 shell 展开。

逐条执行下述命令，不要用 `&&` 串联。本阶段属于 `jira-qa` 技能，阶段一不在本技能执行范围内。

## 输入

```
$ARGUMENTS
```

取第一个非选项参数作为 Jira Issue Key 或 URL。

## 门禁（只读取，不阻断）

1. 阶段一需求准入门禁结论只作为判定输入，不在这里中断流程。
2. 结论不是「通过」、门禁已过期（材料在结论后变化）或找不到门禁结论时，按 `not-satisfied` 结束，不生成方案。
3. 不重复生成需求准入门禁清单，不修改阶段一附件与评论。

## 执行

1. 一次完成前置检查、材料刷新、终态推导：

```bash
node "${CLAUDE_SKILL_DIR}/bin/prepare.mjs" --key <KEY> --stage 2
```

读输出 JSON 的 `status`：

- `REUSED`：材料未变化且上一版方案与 XMind 仍在 Jira，**直接结束**。不要生成、不要上传、不要写评论。
- `BLOCKED`：门禁未通过、已过期或无可用主材料，按第 4 步只发一条说明评论后结束，不生成方案（`reason` 是原因）。
- `SKIPPED`：无可用主需求材料，`prepare` 已写过「不满足执行条件」评论，报告 `reason` 后结束。
- `READY`：继续第 2 步。记下 `runId`、`locate`、`designSpec`、`pending`。

`prepare` 内部已包含 preflight、jira-read、jira-download、locate、design-spec 五步；安装包不完整或任一步失败会整体返回 `status=ERROR`。需要强制重出方案时加 `--force`。

2. 读 `${CLAUDE_SKILL_DIR}/prompts/stage2-plan.md` 与 `stage2-xmind.md`，生成：
   - 测试方案 `.md`（必须含测试目标、范围、非测试范围、策略、重点、环境、数据、入口/出口标准、风险、待测试点评审确认项）；
   - `xmind.spec.json`（树 + notes 四段 + `priority-1..4`）。

3. 写文件并校验：

```bash
node "${CLAUDE_SKILL_DIR}/bin/validate-plan.mjs" --plan <方案.md> --key <KEY>
node "${CLAUDE_SKILL_DIR}/bin/write-xmind.mjs" --spec <xmind.spec.json>
node "${CLAUDE_SKILL_DIR}/bin/validate-xmind.mjs" --xmind <XMind>
```

校验失败按错误修正后重试，最多两次；仍失败写一条「执行异常」后停止，不上传。

4. 组装终态、生成评论并上传：

```bash
node "${CLAUDE_SKILL_DIR}/bin/design-spec.mjs" --key <KEY> --run-id <RUN_ID> \
  --locate ".qa/runs/<RUN_ID>/locate.json" --materials ".qa/runs/<RUN_ID>/materials.json" \
  --plan <方案.md> --xmind <XMind> --xmind-spec <xmind.spec.json>
node "${CLAUDE_SKILL_DIR}/bin/render-design-comment.mjs" --spec ".qa/runs/<RUN_ID>/design.spec.json" \
  --out ".qa/runs/<RUN_ID>/comment.md"
```

`render-design-comment` 输出 `COMMENT=none` 时不要写评论。

- `generated`：

```bash
node "${CLAUDE_SKILL_DIR}/bin/jira-upload.mjs" --key <KEY> --file <方案.md> --file <XMind> \
  --comment-file ".qa/runs/<RUN_ID>/comment.md" --run-id "<RUN_ID>" --stage design
```

- `not-satisfied`：

```bash
node "${CLAUDE_SKILL_DIR}/bin/jira-comment.mjs" --key <KEY> \
  --body ".qa/runs/<RUN_ID>/comment.md" --run-id "<RUN_ID>"
```

5. 汇总本次结果：

```bash
node "${CLAUDE_SKILL_DIR}/bin/result.mjs" --key <KEY> --run-id <RUN_ID> \
  --spec ".qa/runs/<RUN_ID>/design.spec.json" --stage design
```

6. 仅为 `generated` 时提示人工复核，并给出复核标记命令：

```bash
node "${CLAUDE_SKILL_DIR}/bin/mark-reviewed.mjs" --key <KEY> --plan <方案.md> --xmind <XMind> \
  --reviewer <复核人> --jira
```

## 终态

| 终态 | 含义 | Jira 写入 | result |
|---|---|---|---|
| `generated` | 生成新版方案 | 上传方案 + XMind，一条 `QA-测试方案结果vN` | `OK / ok=true` |
| `reused` | 材料未变化，复用上一版 | 零写入 | `OK / reused=true` |
| `not-satisfied` | 门禁未通过/已过期/无可用主材料 | 一条 `QA-测试方案不满足执行条件` | `SKIPPED / applicable=false` |
| 执行异常 | 脚本或环境失败 | 一条 `QA-测试方案执行异常` | `ERROR / ok=false` |

## 约束

- 阶段一由 `review-jira-qa` 负责；本阶段**不得修改阶段一的清单、门禁结论或 Jira 评论**。
- 可重复执行：材料不变则结果不变；材料变化才生成新版本；需要强制重出时在 `locate` 上加 `--force`。
- 输出后停止，等待人工复核方案、正式叶子、页面路径、主要类型和优先级。
- 不生成 Excel、Xray CSV 或测试代码。
- 页面操作路径未确认、层级不完整或存在冲突时，对应叶子标记 `【待确认】`，不计入正式用例数。
- 阶段三的线上运行需要先取回方案与 XMind，本轮未实现，按本地工作区模式执行。
