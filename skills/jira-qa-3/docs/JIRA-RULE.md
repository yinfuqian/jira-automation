# Jira 规则对接说明（阶段三）

本技能在仓库中的注册名是 `jira-qa-3`，只执行 QA 阶段三。本包不实现 HTTP 服务；
Jira 自动化规则由技能平台接收，平台按 `skill_id=jira-qa-3` 启动 Codex Agent 并传入 Jira 链接。

## 平台调用契约

- 技能名：`jira-qa-3`
- 输入：Jira Issue Key 或 `/browse/<KEY>` URL；无需额外入参。
- 平台注入 `JIRA_BASE_URL`、`JIRA_PAT`（兼容 `JIRA_TOKEN`）与 `QA_WORKSPACE`。
- 运行目录：`QA_WORKSPACE`；产物写 `inputs/`、`outputs/`、`.qa/`。
- 对 Jira 只读：只读取单信息、附件列表、附件内容与评论。

```json
{
  "skill_id": "jira-qa-3",
  "jira_url": "https://jira.example.com/browse/KEY-1"
}
```

## 执行流程

```text
prepare.mjs --key <KEY> --stage 3
  ├─ preflight
  ├─ jira-read
  ├─ jira-download
  ├─ fetch-artifacts（Jira → outputs/<scope>/<KEY>/）
  └─ locate --stage cases（校验人工复核）
        ↓
READY / BLOCKED / SKIPPED / ERROR
        ↓ READY
刷新最终方案
        ↓
extract-cases → build-cases → validate-cases
        ↓
写自动化建议 → pack-deliverables --kind cases → cases-result.mjs
```

## 结果判定

技能结束时 stdout 末尾输出 `QA_RESULT_JSON`，并写 `.qa/runs/<runId>/result.json`：

- 成功：`stage=cases`、`status=OK`、`conclusion=已生成`，带 Excel/CSV/zip 路径。
- 缺复核：`prepare` 的 `status=BLOCKED`，记「待人工复核」。
- `status=ERROR` / `ok=false`：执行异常，记「未完成」。

## 失败处理

| 情况 | 退出码 | status / errorCode | 行为 |
|---|---|---|---|
| 缺 `JIRA_BASE_URL`/`JIRA_PAT` | 0 | ERROR / USAGE | 本地 result 留痕，无法回取产物 |
| PAT 无效 | 0 | ERROR / JIRA_AUTH | 本地 result 留痕 |
| 单不存在 | 0 | ERROR / JIRA_NOT_FOUND | 本地 result 留痕 |
| 缺方案/XMind | 0 | BLOCKED | 不生成用例，提示先跑 jira-qa-2 |
| 缺人工复核证据 | 0 | BLOCKED | 不生成用例，提示先复核 |
| 用例校验失败 | 0 | ERROR / VALIDATION | 不产出半成品 |

所有情况均以 exit 0 结束，平台据 `status` / `applicable` / `ok` 判断是否继续。
