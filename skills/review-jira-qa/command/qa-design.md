---
description: 阶段二测试方案与测试用例 XMind：校验阶段一门禁后生成方案与 XMind，上传并停止等待人工复核
argument-hint: <Jira需求Key或URL>
allowed-tools: Read, Write, Edit, Bash(node:*), Glob, Grep
---

执行 QA 阶段二。技能目录用 `${CLAUDE_SKILL_DIR}`，工作区用 `${QA_WORKSPACE}`（缺省当前目录）。

执行前把 `${CLAUDE_SKILL_DIR}` 替换为技能目录绝对路径；`${QA_WORKSPACE:-$PWD}` 由 shell 展开。

逐条执行下述命令，不要用 `&&` 串联；任一脚本输出 `status=ERROR` 或 `status=SKIPPED` 时立即停止，不要继续后续步骤。

## 输入

```
$ARGUMENTS
```

取第一个非选项参数作为 Jira Issue Key 或 URL。

## 门禁（不满足即停止）

1. 最新阶段一清单结论必须为「通过」，且无未关闭项。
2. 阶段一记录的材料附件 ID 必须与当前 Jira 附件一致；不一致视为分析过期，先重跑 `/review-jira-qa`。
3. 不重复生成需求准入门禁清单。

## 执行

1. 刷新材料并校验门禁：

```bash
node "${CLAUDE_SKILL_DIR}/bin/preflight.mjs" --key <KEY> --stage 2
node "${CLAUDE_SKILL_DIR}/bin/jira-read.mjs" --key <KEY>
node "${CLAUDE_SKILL_DIR}/bin/jira-download.mjs" --key <KEY> --run-id <RUN_ID>
node "${CLAUDE_SKILL_DIR}/bin/locate.mjs" --stage design --key <KEY> --materials ".qa/runs/<RUN_ID>/materials.json"
```

2. 读 `${CLAUDE_SKILL_DIR}/prompts/stage2-plan.md` 与 `stage2-xmind.md`，生成：
   - 测试方案 `.md`（必须含测试目标、范围、非测试范围、策略、重点、环境、数据、入口/出口标准、风险）；
   - `xmind.spec.json`（树 + notes 四段 + `priority-1..4`）。

3. 写文件并校验：

```bash
node "${CLAUDE_SKILL_DIR}/bin/validate-plan.mjs" --plan <方案.md> --key <KEY>
node "${CLAUDE_SKILL_DIR}/bin/write-xmind.mjs" --spec <xmind.spec.json>
node "${CLAUDE_SKILL_DIR}/bin/validate-xmind.mjs" --xmind <XMind>
```

4. 上传方案与 XMind，然后停止：

```bash
node "${CLAUDE_SKILL_DIR}/bin/jira-upload.mjs" --key <KEY> --file <方案.md> --file <XMind> --run-id "<RUN_ID>" --stage design
```

5. 提示人工复核，并给出复核标记命令：

```bash
node "${CLAUDE_SKILL_DIR}/bin/mark-reviewed.mjs" --key <KEY> --plan <方案.md> --xmind <XMind> --reviewer <复核人>
```

## 约束

- 阶段一是主任务；阶段二只是独立子任务，**不得修改阶段一的清单、门禁结论或 Jira 评论**。
- 阶段二不写 Jira 评论；异常只写本地 result/日志，不影响主任务输出。
- 输出后停止，等待人工复核方案、正式叶子、页面路径、主要类型和优先级。
- 不生成 Excel、Xray CSV 或测试代码。
- 页面操作路径未确认、层级不完整或存在冲突时，对应叶子标记 `【待确认】`，不计入正式用例数。
