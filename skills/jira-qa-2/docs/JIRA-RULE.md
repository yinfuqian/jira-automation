# Jira 规则对接说明（阶段二）

本技能在仓库中的注册名是 `jira-qa-2`，只执行 QA 阶段二。本包不实现 HTTP 服务；
Jira 自动化规则由技能平台接收，平台按 `skill_id=jira-qa-2` 启动 Codex Agent 并传入 Jira 链接。

## 平台调用契约

- 技能名：`jira-qa-2`
- 输入：Jira Issue Key 或 `/browse/<KEY>` URL；无需额外入参。
- 平台注入 `JIRA_BASE_URL`、`JIRA_PAT`（兼容 `JIRA_TOKEN`）与 `QA_WORKSPACE`。
- 运行目录：`QA_WORKSPACE`；产物写 `inputs/`、`outputs/`、`.qa/`。
- 每次从 Jira 当前最新附件刷新材料，不需要平台传入材料路径。

```json
{
  "skill_id": "jira-qa-2",
  "jira_url": "https://jira.example.com/browse/KEY-1"
}
```

## 执行流程

```text
prepare.mjs --key <KEY> --stage 2
  ├─ preflight
  ├─ jira-read
  ├─ jira-download
  ├─ locate --stage design
  └─ design-spec
        ↓
READY / REUSED / BLOCKED / SKIPPED / ERROR
        ↓ READY
写测试方案 + xmind.spec.json
        ↓
validate-plan → write-xmind → validate-xmind
        ↓
design-spec → render-design-comment
        ↓
jira-upload → result.mjs --stage design → pack-deliverables --kind plan
```

`REUSED` 直接结束，零写入；`BLOCKED` 写一条 `QA-测试方案不满足执行条件` 评论后结束；
`SKIPPED` 记录原因；`ERROR` 记「未完成」并保留本地文件。

## 结果判定

技能结束时 stdout 末尾输出 `QA_RESULT_JSON`，并写 `.qa/runs/<runId>/result.json`：

- `generated`：`stage=design`、`status=OK`、`ok=true`，附件已上传，等待人工复核。
- `reused`：`status=OK`、`reused=true`。
- `not-satisfied`：`status=SKIPPED`、`applicable=false`。
- `status=ERROR` / `ok=false`：执行异常，记「未完成」。

## 失败处理

| 情况 | 退出码 | status / errorCode | 行为 |
|---|---|---|---|
| 缺 `JIRA_BASE_URL`/`JIRA_PAT` | 0 | ERROR / USAGE | 本地 result 留痕，无法上传 |
| PAT 无效 | 0 | ERROR / JIRA_AUTH | 本地 result 留痕 |
| 单不存在 | 0 | ERROR / JIRA_NOT_FOUND | 本地 result 留痕 |
| 门禁未通过/过期 | 0 | BLOCKED / not-satisfied | 写说明评论，不生成方案 |
| 方案/XMind 校验失败 | 0 | ERROR / VALIDATION | 不上传、不产出半成品 |
| 上传/评论失败 | 0 | ERROR / JIRA_WRITE | 本地文件保留 |

所有情况均以 exit 0 结束，平台据 `status` / `applicable` / `ok` 判断是否继续。
