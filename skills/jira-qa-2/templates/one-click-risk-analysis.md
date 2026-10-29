# 一键执行口令：阶段一｜需求准入门禁

本模板只覆盖阶段一，不自动进入测试方案、XMind、Excel 或 Xray CSV。
契约以 `prompts/contract.md` 为准，判定规则以 `prompts/stage1-gate.md` 为准。

## 标准口令

```text
/review-jira-qa <Jira需求Key或URL>
```

## 输入

- Jira Issue Key 或完整 `/browse/<IssueKey>` URL：必需。
- 主需求材料：Jira 单当前最新需求附件，由 `prepare.mjs` 自动下载。
- 无附件但有 Jira 需求描述时，以 `<KEY>-需求描述.md` 为主材料。
- 无需求侧材料时按 `SKIPPED` 处理，不写门禁结论。

## 执行步骤

1. 执行 `prepare.mjs --key <KEY> --stage 1`，读取 `status`：
   - `READY`：继续；
   - `REUSED`：复用上一版结论，零写入，结束；
   - `SKIPPED`：不适用，写「评审不适用」评论后结束；
   - `ERROR`：记「未完成」，交还调用方。
2. 核读 `materials-digest.md` 与全部需求侧材料，按 B1–B7 / C1–C6 逐项判定。
3. 对全部 active 需求侧材料运行 `scan.mjs`，生成 `candidates.json`。
4. 写 `gate.spec.json`：`checks` 覆盖 13 项，`coverage` 覆盖全部 AC，
   `scans` 逐条回应候选，`failures` 带原文 `facts[].quote` 与 `criteria`。
5. `render-gate.mjs` 生成 HTML 与评论。
6. `validate-gate.mjs --coverage-required --candidates --source` 机械校验。
7. `jira-upload.mjs` 上传 HTML，`jira-comment.mjs` 同步 QA 评论，`result.mjs` 输出结果。

## 验收清单

- [ ] 结论只有「通过 / 暂不通过」；
- [ ] 不通过项编号 1..N 连续，类型只有「材料冲突 / 材料未明确」；
- [ ] `checks` 覆盖 B1–B7、C1–C6 共 13 项；
- [ ] `coverage` 覆盖 `coverage-required.json` 全部 AC；
- [ ] `scans` 逐条回应 `candidates.json`；
- [ ] 每条 failure 的 `facts[].quote` 能在材料原文中检索到；
- [ ] 未生成测试方案、测试点、XMind、Excel、CSV 或测试代码；
- [ ] 未修改 Jira 状态、需求描述或产品材料。

## 强制门禁

- 阶段一结束即停止，不自动接续阶段二/三。
- 结论「暂不通过」仍是业务完成（exit 0），由父技能决定是否回退。
- 所有脚本统一 exit 0；`status=SKIPPED` 记为「不适用」，`status=ERROR` / `ok=false`
  记为「未完成」，都不得阻塞父流程。
