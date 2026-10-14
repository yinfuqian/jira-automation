# Jira 取数与写回细节（缺陷单门禁）

面向 `jira-gate-bug` skill 的实现说明。基础地址：`https://jira.in.wezhuiyi.com`（Jira 10.3.9 Server / Data Center）。

## 1. 运行通道

- **服务器容器 / `codex exec`（默认）**：直接 `node scripts/jira-cli.mjs <命令>`；`jira.mjs` 内的 `request()` 使用 Node 原生 `fetch`。
- **桌面 Codex（`node_repl`）**：`await import("file:///<技能目录>/scripts/jira.mjs")`；`node_repl` 内没有 `process`（读不到环境变量）、不支持静态 `import`，因此令牌用 `--token` 或令牌文件提供；改了脚本后用 `?v=时间戳` 强制重载。
- 不要用 `curl` / `Invoke-WebRequest` 访问 Jira：技能脚本统一处理认证头与错误信息，避免出现 PowerShell TLS / XSRF 之类的问题。

## 2. 认证

令牌来源（按优先级）：`--token` → `JIRA_TOKEN` 环境变量（部署时由 `.env` 注入 worker）→ 令牌文件。
容器内后端会注入 `SKILLRUN_ENV_LOCKED=1`，此时只认 `JIRA_TOKEN` / `JIRA_BASE_URL` 两个环境变量，不再读取下面的令牌文件：

1. `<技能目录>/.jira-token`、`.jira-token.txt`、`jira-token.txt`
2. `<技能目录>/../jira-token.txt`
3. `~/.codex/jira-token.txt`、`~/.codex/jira/token.txt`
4. `~/.codex/jira/config.json`：`{"token":"...","baseUrl":"https://jira.in.wezhuiyi.com"}`

请求头固定：`Accept: application/json`、`X-Atlassian-Token: no-check`，认证用 `Authorization: Bearer <token>`（配置 `auth=basic` 时用 Basic）。

## 3. 用到的接口

| 用途 | 方法与路径 |
|---|---|
| 连通性 | `GET /rest/api/2/serverInfo`（无需认证） |
| 当前账号 | `GET /rest/api/2/myself`（部分站点对普通账号返回 401，不代表令牌失效） |
| 缺陷单全量字段 | `GET /rest/api/2/issue/{KEY}?expand=names,renderedFields,schema` |
| 评论全量 | `GET /rest/api/2/issue/{KEY}/comment` |
| 子任务兜底 | `GET /rest/api/2/search?jql=parent = {KEY}` |
| 写评论 | `POST /rest/api/2/issue/{KEY}/comment`，Body `{"body":"..."}` |
| 改评论 | `PUT /rest/api/2/issue/{KEY}/comment/{id}` |
| 附件下载 | `GET {attachment.content}`（带同一认证头） |
| **附件上传** | `POST /rest/api/2/issue/{KEY}/attachments`，`multipart/form-data`，字段名 `file`，**必须带 `X-Atlassian-Token: no-check`** |
| 可用流转 | `GET /rest/api/2/issue/{KEY}/transitions` |
| 执行流转 | `POST /rest/api/2/issue/{KEY}/transitions`，Body `{"transition":{"id":"<id>"}}` |
| 设置经办人 | `PUT /rest/api/2/issue/{KEY}/assignee`，Body `{"name":"<登录名>"}` |
| 字段列表 | `GET /rest/api/2/field`（用于定位门禁字段） |
| 写字段 | `PUT /rest/api/2/issue/{KEY}`，Body `{"fields":{...}}` |

## 4. 门禁字段（可选）

默认候选名（`jira-config.json` 可覆盖）：

| 用途 | 默认字段名 | 取值 |
|---|---|---|
| 预检结论 | `缺陷单预检结论` | `通过` / `不通过` / `未检查` |
| 自查结论 | `缺陷单自查结论` | `有` / `无` |
| 分析报告 | `缺陷单分析报告` | 报告文件名或链接 |
| 预检时间 | `缺陷单预检时间` | ISO 时间 |

- 字段不存在时 `setGateResult()` 返回 `{skipped:true, reason}`，**不抛异常**：评论与附件仍然是有效交付物。
- 单选字段写 `{value:"通过"}`，其它类型写字符串。
- 工作流门禁由 Jira 管理员配置条件/校验器读取该字段，AI 不直接流转状态。

## 5. 可选流转（打回）

默认**不流转状态**。只有 `jira-config.json` 配置了 `rejectTransitionName`（例如 `"打回"`）时才执行：

```bash
node scripts/jira-cli.mjs transitions <KEY|URL>
node scripts/jira-cli.mjs transition <KEY|URL> --name 打回
```

流转名必须与 Jira 中实际可用流转完全一致，脚本会先列出并匹配，找不到时报错并给出可用列表。

打回时 `transitionIssue()` 会先记录流转前的原经办人；流转后再次读取工单，如果工作流 post-function 或其它规则把经办人改成了门禁账号，再调用 `PUT /assignee` 恢复原经办人。原经办人为空时跳过恢复。经办人恢复失败会抛错，执行结果不能当作打回成功。

## 6. 附件与报告

- 报告由 `scripts/analysis-cli.mjs analyze --out <路径>` 生成，单文件内联样式，无外网依赖。
- 上传：`node scripts/jira-cli.mjs attach <KEY> --file <报告.html> --filename 故障分析报告-<KEY>.html`。
- Jira 附件有大小限制：报告体积过大时先压缩日志输入，或拆分为多条评论说明。

## 7. 实测注意事项

1. `subtasks` 字段是复数；为空时脚本会再用 JQL `parent = {KEY}` 兜底。
2. 评论数超过首页返回条数时，`getIssue()` 会自动再拉一次 `/comment` 全量接口。
3. 附件下载 404 说明附件已被删除：按「附件缺失」如实记录，不要静默跳过。
4. `setGateResult()` 写入要求字段在该问题类型的编辑屏幕上；字段存在但没有屏幕权限时会返回 HTTP 400，此时按 `skipped` 处理，不要重试写字段。
