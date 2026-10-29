---
description: QA 阶段三：从 Jira 取回人工复核后的基线，生成 Excel 与 Xray CSV，交付物打包到 outputs/
argument-hint: <Jira需求Key或URL>
allowed-tools: Read, Write, Edit, Bash(node:*), Bash(python:*), Glob, Grep
---

执行 QA 阶段三。技能目录用 `${CLAUDE_SKILL_DIR}`，工作区用 `${QA_WORKSPACE}`（缺省当前目录）。

执行前把 `${CLAUDE_SKILL_DIR}` 替换为技能目录绝对路径；`${QA_WORKSPACE:-$PWD}` 由 shell 展开。
逐条执行 `SKILL.md` 中的命令，不要用 `&&` 串联。

## 输入

```
$ARGUMENTS
```

取第一个非选项参数作为 Jira Issue Key 或 URL。

## 执行顺序

1. `prepare.mjs --key <KEY> --stage 3`：内部回取 Jira 产物并校验人工复核，读 `status`。
   `READY` 继续，`BLOCKED` 记「待人工复核」，`SKIPPED` 记录原因，`ERROR` 记未完成。
2. 读 `prompts/stage3-cases.md` 刷新最终方案（只做不改变业务含义的修正）。
3. `extract-cases` → `build-cases`。
4. `validate-cases` 必须 100% 通过。
5. 写 `<KEY>-测试用例结果.txt`（用例总数、类型与优先级分布、自动化建议）。
6. `pack-deliverables.mjs --kind cases` → `cases-result.mjs`。

## 约束

- 只执行阶段三，不重新设计测试点，不修改人工复核后的用例边界与定级。
- 不写 Jira：不上传附件、不写评论、不改状态、不删除产品材料。
- 缺方案/XMind 或复核评论时停止，不产出半成品。
- 所有脚本统一 exit 0，按 `status` / `applicable` / `ok` 判断后续动作。
