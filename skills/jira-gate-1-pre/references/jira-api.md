# Jira 取数与写评论细节（G1-Pre 专用）

本文件只服务于 `jira-gate-1-pre`：读单据、下载附件、写一条 G1-Pre 评论。
**不要在本技能里调用流转、改字段、上传或删除附件接口。**

## 1. 通道与执行位置

- 容器 / `codex exec`：直接用 shell 执行 `scripts/jira-cli.mjs` 与 `scripts/g1-pre.mjs`。
- 桌面 `node_repl`：没有 `process`、不支持静态 `import`，用 `await import("file:///<SKILL_DIR>/scripts/jira.mjs")` 动态加载。
- PowerShell 沙箱可能无法建立 TLS 连接；遇到 `SSL connection could not be established` 时改走 `node_repl` 或容器内 Node。
- `node_repl` 会缓存模块；改脚本后用 `import("file:///...jira.mjs?v=时间戳")` 强制重载。

## 2. 认证

- 容器内由后端注入 `SKILLRUN_ENV_LOCKED=1` 时，**只认** `JIRA_TOKEN` 与 `JIRA_BASE_URL`，不接受命令行令牌、令牌文件或其它站点。
- 桌面端未锁定标记时，按「入参 `token` / `--token` → 环境变量 `JIRA_TOKEN` → 令牌文件」解析。
- 请求头：`Authorization: Bearer <token>`；固定头 `Accept: application/json`、`X-Atlassian-Token: no-check`。
- 非公开项目匿名读取会返回 `HTTP 401 您没有查看特定问题的权限；必须登录`；写评论必须认证。
- 执行前先跑 `node scripts/jira-cli.mjs selftest`，确认令牌、站点与账号。

令牌文件候选（桌面端）：

1. `<skill 目录>/.jira-token`、`.jira-token.txt`、`jira-token.txt`
2. `<skill 目录>/../` 下同名文件
3. `~/.codex/jira-token.txt`
4. `~/.codex/jira/token.txt`
5. `~/.codex/jira/config.json`（`{"token":"...","baseUrl":"..."}`）

## 3. 常用接口

| 用途 | 方法与路径 |
|---|---|
| 连通性 | `GET /rest/api/2/serverInfo`（无需认证） |
| 当前账号 | `GET /rest/api/2/myself` |
| 单据全量字段 | `GET /rest/api/2/issue/{KEY}?expand=names,renderedFields,schema` |
| 写评论权限预检 | `GET /rest/api/2/mypermissions?issueKey={KEY}`，看 `ADD_COMMENTS` |
| 全部评论 | `GET /rest/api/2/issue/{KEY}/comment` |
| 写评论 | `POST /rest/api/2/issue/{KEY}/comment`，Body `{"body":"..."}` |
| 改评论 | `PUT /rest/api/2/issue/{KEY}/comment/{id}` |
| 附件下载 | `GET {attachment.content}`，带同一认证头 |
| 按父单查子任务 | `GET /rest/api/2/search?jql=parent%20%3D%20{KEY}` |

错误响应形如 `{"errorMessages":["..."],"errors":{...}}`，`jira.mjs` 会拼成可读错误。

## 4. 字段映射

`expand=names` 返回 `customfield_xxx → 中文名`，`jira.mjs` 用它生成 `customFields`。G1-Pre 会用到：

- `客户名称`
- `产品归属`
- `问题发现产品` / `发现产品`
- `关联问题`

标准字段：`issuetype`、`summary`、`description`、`status`、`priority`、`duedate`、`reporter`、`assignee`、`labels`、`components`、`attachment`、`comment`。

`renderIssueMarkdown()` 会跳过 `Rank`、`Development`、`Story point estimate` 等噪音字段；G1-Pre 使用 `g1-pre.mjs context` 输出的结构化 JSON，不依赖该 Markdown 渲染。

## 5. 附件解析

```bash
node scripts/jira-cli.mjs attachments "<KEY|URL>" "work/jira-gate/<KEY>/attachments"
python scripts/extract.py "work/jira-gate/<KEY>/attachments" --out "work/jira-gate/<KEY>/attachments.txt"
```

- 支持 `docx / pdf / xlsx / pptx / txt / md / json / xml / csv / html`。
- 压缩包（`.zip / .rar / .7z / .tar / .tar.gz / .tgz / .gz / .bz2 / .xz`）先解压到 `__unpacked/` 再递归解析。
- `.rar` 按 `bsdtar → 7z → 7zz → 7za → unar → unrar → tar` 降级；容器镜像已预装，**不要联网安装**。
- 解压失败输出 `[压缩包解压失败]` 与尝试过的命令；必须记为「附件无法解析」，不能当成空内容，也不能臆测包内内容。
- 引用证据写到包内路径，例如 `附件.zip/PRD.md §4.2`。

## 6. 写评论权限预检

落笔前如需确认权限，可调用：

```javascript
const p = await j.request("GET", `/rest/api/2/mypermissions?issueKey=${key}`);
// 只关心 ADD_COMMENTS；本技能不需要 TRANSITION_ISSUES / CREATE_ATTACHMENTS
```

`ADD_COMMENTS=false` 时不要反复重试，直接报告缺少评论权限，并把 `.skill-result.json` 的 `conclusion` 写成「失败」。

## 7. 评论写入实操

- 评论正文由 `g1-pre.mjs comment` 生成，不要手写替代；需要预览时先不加 `--write`。
- 不通过评论必须包含报告人提及 `[~登录名]`，登录名取自 `issue.reporter.name`，不要用显示名。
- 写入后读回确认：

```javascript
const created = await j.addComment(key, body);
const back = await j.getIssue(key);
const saved = back.comments.find((item) => item.id === String(created.id));
```

- Jira Wiki Markup：标题用 `h3.` / `h4.`，加粗标签用 `*当前问题：*`；不要提交 Markdown 表格；不要出现未转义的 `{color}`、`{{monospace}}` 等宏；提及统一用 `[~login]`。
- 评论中**不得**出现规则版本、内容指纹、sha256 或「G1-Pre 指纹」。幂等改为比对「最新一条 G1-Pre 评论正文」，本轮渲染结果一致时不重复写入；判重命令使用 `history <KEY|URL> --file <result.json>`。

## 8. 故障排查

| 现象 | 原因与处理 |
|---|---|
| `SSL connection could not be established` | 在 PowerShell 沙箱里访问 Jira；改到 `node_repl` 或容器 Node。 |
| `process is not defined` | 在 `node_repl` 里用了静态 `import` 或 `process`；改为 `await import()`。 |
| `HTTP 401 必须登录` | 令牌缺失/失效或访问非公开项目；先 `selftest`。 |
| `HTTP 403` 且带 XSRF 提示 | 缺少 `X-Atlassian-Token: no-check`。 |
| 附件下载 404 | 附件已被删除；按「附件缺失/不可读」记录，不要静默跳过。 |
| `[压缩包解压失败]` | 环境缺少解压命令；记为「附件无法解析」并报告，不要自行安装。 |
| 评论数与 `fields.comment.total` 不一致 | `getIssue()` 会自动再拉一次 `/comment` 全量接口。 |
| `[~xxx]` 没有渲染成 @ 人 | 用了显示名；必须用登录名 `issue.reporter.name`。 |
| `g1-pre.mjs history` 返回 `duplicate=false` | 没有传 `--file`，或最新一条 G1-Pre 评论正文与本轮渲染结果不同；按需要写入新评论。 |
| `contentHash` 校验失败 | 没有先跑 `g1-pre.mjs hash`，或手工填了占位值。 |

## 9. 硬边界

- 不调用 `POST /issue/{KEY}/transitions`。
- 不调用 `PUT /issue/{KEY}`（改字段）。
- 不调用 `POST /issue/{KEY}/attachments` 与 `DELETE /attachment/{id}`。
- 不调用 `gate-set` / `attach` / `attach-prune`。
- 只允许：读取、下载附件、写一条 G1-Pre 评论（幂等跳过时连评论都不写）。
