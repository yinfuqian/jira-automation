---
description: QA 阶段二：生成测试方案与测试点 XMind，上传 Jira 并等待人工复核，交付物打包到 outputs/
argument-hint: <Jira需求Key或URL> [--force]
allowed-tools: Read, Write, Edit, Bash(node:*), Glob, Grep
---

执行 QA 阶段二。技能目录用 `${CLAUDE_SKILL_DIR}`，工作区用 `${QA_WORKSPACE}`（缺省当前目录）。

执行前把 `${CLAUDE_SKILL_DIR}` 替换为技能目录绝对路径；`${QA_WORKSPACE:-$PWD}` 由 shell 展开。
逐条执行 `SKILL.md` 中的命令，不要用 `&&` 串联。

## 输入

```
$ARGUMENTS
```

取第一个非选项参数作为 Jira Issue Key 或 URL。`--force` 表示强制重出方案。

## 执行顺序

1. `prepare.mjs --key <KEY> --stage 2`，读 `status`：`READY` 继续，`REUSED` 直接结束，
   `BLOCKED` 只发不满足条件评论，`SKIPPED` 记录原因，`ERROR` 记未完成。
2. `READY` 时读 `prompts/stage2-plan.md`、`prompts/stage2-xmind.md` 生成方案与 `xmind.spec.json`。
3. `validate-plan` → `write-xmind` → `validate-xmind`。
4. `design-spec` → `render-design-comment` → `jira-upload`（或 `not-satisfied` 时 `jira-comment`）。
5. `result.mjs --stage design` → `pack-deliverables.mjs --kind plan`。
6. 提示人工复核，给出 `mark-reviewed.mjs --jira` 命令。

## 约束

- 只执行阶段二，不进入阶段一或阶段三。
- 不生成 Excel、Xray CSV 或测试代码。
- 不改 Jira 状态，不修改需求描述，不删除产品材料。
- 所有脚本统一 exit 0，按 `status` / `applicable` / `ok` 判断后续动作。
