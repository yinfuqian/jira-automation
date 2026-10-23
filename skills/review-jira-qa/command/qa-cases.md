---
description: 阶段三 Excel 与 Xray CSV：校验人工复核标记，按最终 XMind 一叶一用例生成 Excel 与 CSV
argument-hint: <Jira需求Key或URL>
allowed-tools: Read, Write, Edit, Bash(node:*), Bash(python:*), Glob, Grep
---

执行 QA 阶段三。技能目录用 `${CLAUDE_SKILL_DIR}`，工作区用 `${QA_WORKSPACE}`（缺省当前目录）。

执行前把 `${CLAUDE_SKILL_DIR}` 替换为技能目录绝对路径；`${QA_WORKSPACE:-$PWD}` 由 shell 展开。

逐条执行下述命令，不要用 `&&` 串联；任一脚本输出 `status=ERROR` 或 `status=SKIPPED` 时立即停止，不要继续后续步骤。

## 输入

```
$ARGUMENTS
```

取第一个非选项参数作为 Jira Issue Key 或 URL。

## 门禁（不满足即停止）

1. 人工复核后的测试方案与人工审核后的 XMind 必须存在，且位于同一 `outputs/<scope>/<KEY>/`。
2. 复核标记（`mark-reviewed.mjs` 写入）必须与当前文件哈希一致；文件变化后需重新复核。
3. 多版本无法唯一确定时停止，不猜。

## 执行

1. 定位并校验：

```bash
node "${CLAUDE_SKILL_DIR}/bin/preflight.mjs" --key <KEY> --stage 3
node "${CLAUDE_SKILL_DIR}/bin/locate.mjs" --stage cases --key <KEY>
```

2. 读 `${CLAUDE_SKILL_DIR}/prompts/stage3-cases.md`，按最终 XMind 刷新测试方案（只刷新，不重新设计）。

3. 抽取并生成：

```bash
node "${CLAUDE_SKILL_DIR}/bin/extract-cases.mjs" --xmind <XMind> --key <KEY> --name <需求名称>
node "${CLAUDE_SKILL_DIR}/bin/build-cases.mjs" --cases <cases.json> --key <KEY>
node "${CLAUDE_SKILL_DIR}/bin/validate-cases.mjs" --cases <cases.json> --xlsx <xlsx> --csv <csv> --xmind <XMind>
```

4. 输出自动化建议；Excel/CSV 不上传 Jira。

## 约束

- 阶段一是主任务；阶段三只是独立子任务，**不得修改阶段一的清单、门禁结论或 Jira 评论**。
- 阶段三不写 Jira 评论、不上传 Excel/CSV；异常只写本地 result/日志。
- 一叶一用例，不跨叶合并、不拆分、不重新排序、定级或分类。
- 只允许不改变业务含义的格式、编号、错别字、空格和标点修正；业务语义问题退回阶段二。
- 机械一致性必须 100% 通过；失败时停止并报告，不产出半成品。
- `【待确认】` 叶子不计入正式用例。
