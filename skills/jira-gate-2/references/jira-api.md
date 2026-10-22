# Jira API 参考（jira-gate-2）

面向 `jira-gate-2` skill 的实现说明。站点为 Jira 10.3.9 Server / Data Center（生产站点名"Jira-追一研发管理系统"）。

## 1. 两条取数通道

| 通道 | 适用环境 | 入口 |
|---|---|---|
| A. REST | 桌面 Codex（`node_repl`）或服务器容器 | `scripts/jira.mjs`（动态导入）或 `scripts/jira-cli.mjs`（shell） |
| B. 页面 | 没有令牌、但有已登录的应用内浏览器 | `cua.createBrowserTab("iab", issueUrl)` + `tab.playwright` |

两条通道的判定标准完全一致；通道 B 的结论要注明"基于页面内容检查"。

## 2. 认证

- 令牌只走 `Authorization: Bearer <token>`，不要打印、回显或写进报告文件。
- 容器内后端注入 `SKILLRUN_ENV_LOCKED=1` 时，`jira.mjs` / `jira-cli.mjs` **只认** `JIRA_TOKEN` 与 `JIRA_BASE_URL` 环境变量，不接受 `--token` / `--base-url` 与令牌文件，访问其它站点会直接报错。
- 桌面端未锁定时的解析顺序：入参 → 环境变量 → 技能目录的 `.jira-token` / `.jira-token.txt` / `jira-token.txt` → `~/.codex/jira-token.txt` → `~/.codex/jira/config.json`（支持 `{"token":"...","baseUrl":"..."}`）。
- `selftest` 的「校验令牌」步骤返回 401 只表示令牌无权访问 `/myself`；只要「访问 Jira」成功且 `get` 能取到单子，就继续执行。

## 3. 常用接口

| 用途 | 方法与路径 |
|---|---|
| 读单子 | `GET /rest/api/2/issue/{KEY}?expand=names,renderedFields,schema` |
| 读评论全量 | `GET /rest/api/2/issue/{KEY}/comment` |
| 读字段定义 | `GET /rest/api/2/field` |
| 读可用流转 | `GET /rest/api/2/issue/{KEY}/transitions` |
| 新建评论 | `POST /rest/api/2/issue/{KEY}/comment`，body `{"body":"..."}` |
| 更新评论 | `PUT /rest/api/2/issue/{KEY}/comment/{COMMENT_ID}` |
| 执行流转 | `POST /rest/api/2/issue/{KEY}/transitions`，body `{"transition":{"id":"..."}}` |
| 写字段 | `PUT /rest/api/2/issue/{KEY}`，body `{"fields":{...}}` |
| 读权限 | `GET /rest/api/2/mypermissions?permissions=ADD_COMMENTS,TRANSITION_ISSUES` |

命令行入口（容器内无需 `node_repl`）：

```bash
node scripts/jira-cli.mjs selftest
node scripts/jira-cli.mjs precheck <KEY|URL>
node scripts/jira-cli.mjs get <KEY|URL>
node scripts/jira-cli.mjs json <KEY|URL>
node scripts/jira-cli.mjs attachments <KEY|URL> <目录>
node scripts/jira-cli.mjs comment <KEY|URL> --file <文件>
node scripts/jira-cli.mjs comment-update <KEY|URL> <评论ID> --file <文件>
node scripts/jira-cli.mjs reviews <KEY|URL>
node scripts/jira-cli.mjs gate-get <KEY|URL>
node scripts/jira-cli.mjs gate-set <KEY|URL> --file <JSON>
node scripts/jira-cli.mjs transitions <KEY|URL>
node scripts/jira-cli.mjs flow-owner <KEY|URL>
node scripts/jira-cli.mjs transition <KEY|URL> --to <目标状态>
node scripts/jira-cli.mjs attach-list <KEY|URL>
```

## 4. 字段映射

`getIssue()` 返回的规范化字段：

- `issueType`：问题类型，G2 **只处理「故事」**（英文站点 `Story` 视为同一类型）。
- `status`：当前状态，G2 的入口参考是「开发设计中」；通过不改变状态，退回补充时流转到「开发设计中」。
- `summary` / `description` / `priority` / `assignee` / `reporter` / `creator`。
- `attachments`：附件列表（`filename` / `size` / `mimeType` / `content`）。
- `comments`：评论列表（`id` / `body` / `author` / `created`）。
- `customFields`：按字段中文名展开的自定义字段。
- `timeTracking` / `originalEstimateSeconds` / `remainingEstimateSeconds`。

自定义字段名以站点实际配置为准；`Rank`、`Global Rank`、`Development`、`Story point estimate`、`Σ Progress` 是噪音字段，渲染时跳过。

## 5. 浏览器兜底

1. `cua.createBrowserTab("iab", issueUrl)` 打开单子。
2. `tab.playwright.evaluate(() => document.body.innerText)` 取正文。
3. 确认页面「类型」是「故事」；不是就立即停止。
4. 附件通过点击链接触发下载，再对下载目录跑 `scripts/extract.py`。
5. 评论在页面评论区输入并提交。

Jira 的 `/rest/...` 地址在浏览器里会被拦截（`ERR_BLOCKED_BY_CLIENT`），**不要在浏览器里访问 REST 地址**。

## 6. 实测踩过的坑

| 现象 | 处理 |
|---|---|
| `SSL connection could not be established` | 在 PowerShell 里直接访问 Jira；改用 `node_repl` 或 `jira-cli.mjs` |
| `process is not defined` | `node_repl` 里不要静态 `import`、不要引用 `process`；用 `await import()` |
| `HTTP 401 必须登录` | 令牌缺失/失效或访问了非公开项目；先 `selftest`，必要时走浏览器通道 |
| `HTTP 403` 且带 XSRF 提示 | 写请求缺少 `X-Atlassian-Token: no-check` 头 |
| 附件下载 404 | 附件已被删除；按"附件缺失"记录，不要静默跳过 |
| 输出里有 `[压缩包解压失败]` | 如实记为"附件无法解析"，不要安装软件、不要臆测包内内容 |
| 评论数与 `fields.comment.total` 不一致 | `getIssue()` 会自动再拉 `/comment` 全量接口 |
| `{ok:false, reason:"no-transition"}` | 当前状态没有通向目标状态的流转；按 `available` 如实记录，不要改用其他状态 |
| 流转返回 403 | 缺 `TRANSITION_ISSUES` 权限；跳过流转，评论写「未流转（缺少流转权限）」 |
| `[~xxx]` 没渲染成 @ 人 | 用了显示名；要用登录名（如 `liu.huan`），可用 `flow-owner` 取 `mention` |

## 7. 写评论前的权限预检

`mypermissions` 关注两项：

- `ADD_COMMENTS`：为 `false` 时不要写评论，直接告知用户缺权限，不要反复重试。
- `TRANSITION_ISSUES`：为 `false` 时跳过退回补充的流转，评论写「未流转（缺少流转权限）」；结论为通过时不调用流转接口。

`jira-cli.mjs` 没有单独的 `mypermissions` 子命令时，用 `jira.mjs` 的 `mypermissions()`（`node_repl` / 动态导入）或直接调用上面的 REST 接口。

## 8. 评论写入实操

- 正文先落本地文件，再用 `--file` 提交，避免多行 wiki 标记与引号转义问题。
- 评论用 Jira wiki 标记：`h3.` / `h4.` / `h5.` 标题、`*粗体*`、行首 `* ` 无序列表、行首 `# ` 有序列表、`(/)` 绿勾、`(x)` 红叉、`{{等宽}}`。
- 不要提交 Markdown 表格；需要表格时用 `||表头||` + `|单元格|`。
- 评论正文避免出现未转义的花括号组合（`{color}` 之类会被当作宏）。
- 结论只以评论留痕，不生成报告文档、不上传附件；本地仅保留附件与解析中间文件。

## 9. 交付顺序、类型预检与状态流转

**类型预检（强制先做）**：

```bash
node scripts/jira-cli.mjs precheck <KEY|URL>
# {"ok":true,"key":"...","issueType":"故事","expected":"故事","supported":true,"status":"开发设计中"}
```

`supported=false` 或类型为空时立即停止，不写评论、不回写字段、不流转。

**状态流转**：

- 结论「通过」：**不流转状态**，保持当前状态，允许进入 OpenSpec `propose`；不 @ 人。
- 结论「退回补充」：流转到 **开发设计中**，并在评论里 @ 流转人。

**流转人**：本站在 `/rest/api/2/field` 里没有名为「流转人」的字段，确认口径是**取经办人 `assignee`**；经办人为空时按 `assignee → reporter → creator` 回退，并在对话摘要里写明实际来源。需要长期换口径时，在技能目录的 `jira-config.json` 里用 `flowOwnerField` 指定，不要改脚本。

**@ 写法**：Jira Server / DC 按登录名引用，`[~liu.huan]` 渲染为「刘欢」。`flow-owner <KEY|URL>` 直接返回拼好的 `mention`。

**目标状态名**：退回补充固定为「开发设计中」；结论为通过时不得调用流转接口。**不要写死流转 id**：不同项目工作流的流转 id 与流转名都可能不同，`transitionToStatus()` 按目标状态名匹配（先比 `to`，再比 `name`）。

```bash
node scripts/jira-cli.mjs transitions <KEY|URL>              # 只读：看当前可用流转
node scripts/jira-cli.mjs flow-owner <KEY|URL>               # 只读：看流转人与 @ 写法
node scripts/jira-cli.mjs transition <KEY|URL> --to 开发设计中 # 仅退回补充
```

**降级**：匹配不到流转时返回 `{ok:false, reason:"no-transition", status, target, available}`，`available` 是当前可用流转列表；按 `SKILL.md` §9 记「未流转（该单当前状态无目标状态流转）」，并在对话里列出 `available` 交人工处理，不要改用其他状态。已在目标状态时返回 `{ok:true, skipped:true, reason:"already-in-target"}`，不重复发起流转。

**顺序固定**：**评论（含 @）→ 门禁字段 → 流转（仅退回补充）**，流转放最后。通过时不流转，评论里的「状态流转：」固定写「未流转（通过，保持当前状态）」；退回补充时评论先按预期写，流转跑完必须核对返回值，任一非成功分支都用 `comment-update` 改成实际措辞。

## 10. G2 门禁字段（Jira 管理端配置）

### 10.1 为什么不能在工作流里直接调 AI

工作流校验器/条件必须是**同步、确定性、亚秒级**的；AI 准出是异步、非确定性的，还要读附件和代码。因此正确架构是两段式：

1. **AI 准出**（本 skill）：读需求单与技术方案 → 判定 → 写评论 → 用 `setGateResult()` 把结论回写到 Jira 字段。
2. **工作流门禁**（Jira 配置）：流转校验只读那个字段，不调用 AI。

### 10.2 需要 Jira 管理员做的事

1. **建自定义字段**（管理 → 问题 → 自定义字段），类型建议：

| 字段名 | 类型 | 取值 |
|---|---|---|
| `G2准入结论` | 单选列表 | `未检查` / `通过` / `不通过` / `豁免` |
| `G2预检时间` | 日期时间 | — |

   字段名可自定义，但要和技能目录 `jira-config.json` 里的 `gateResultField` / `gateCheckedAtField` 保持一致。

2. **加入屏幕**：把上述字段加到 `故事` 的**查看**和**编辑**屏幕上（REST 写字段要求字段在该问题类型的编辑屏幕上）。
3. **加流转门禁**：在人工推进的「开发设计中 → 开发中」这条流转上二选一（本技能通过时不自动流转，由人工按门禁结果决定是否推进）：

| 方案 | 配置 | 效果 | 依赖 |
|---|---|---|---|
| A. 隐藏按钮 | 条件（Condition）→ 内置 **Value Field**，`G2准入结论 = 通过` | 不满足时按钮直接不显示 | 无，Jira 原生支持 |
| B. 拦截并报错 | 校验器（Validator）→ ScriptRunner `Simple Scripted Validator` / JMWE 条件校验 | 按钮可见，点击后报错提示去评论区看缺口 | 需要 ScriptRunner 或 JMWE |

   推荐 **B**（能告诉用户为什么不能流转，整改闭环更短）；两种都不影响 REST 与批量流转路径，因为条件和校验器在所有路径上都会执行。

### 10.3 两个必须堵的漏洞

1. **结论可被手工绕过**：任何有「编辑问题」权限的人都能把 `G2准入结论` 手动改成 `通过`。
   - 轻量缓解：字段配置里把 `G2准入结论` 设为只读，让 UI 改不了（REST 仍可写，需实测确认）。
   - 强校验：校验器里额外读该字段的改动记录，要求**最后一次修改者必须是 AI 专用账号**，否则拒绝流转。
2. **结论会过期**：技术方案或 PRD 改版后，字段仍挂着 `通过`。
   - 缓解：用 ScriptRunner 监听器（Issue Updated），在**附件增删改、描述变更或状态回退**时把 `G2准入结论` 重置为 `未检查`，强制重新准出。

### 10.4 相关函数

```javascript
const rf = await j.resolveGateFields();     // 探测字段是否存在：{ fields, missing, configSource }
const g  = await j.getGateResult("CALL-1940");  // 读回当前值
const r  = await j.setGateResult("CALL-1940", { result: "不通过" });
const d  = await j.setGateResult("CALL-1940", { result: "通过" }, { dryRun: true });  // 只算不写
```

流转相关函数：

```javascript
const ts = await j.listTransitions("CALL-1446");                  // 当前可用流转 [{id, name, to}]
const fo = await j.resolveFlowOwner(issue);                       // 流转人与 @ 写法 {name, mention, field}
const back = await j.transitionToStatus("ZYSQ-95", "开发设计中");  // 仅退回补充时流转
```

## 11. 故障排查

| 现象 | 原因与处理 |
|---|---|
| `precheck` 返回 `supported:false` | 单据类型不是「故事」；立即停止，不做任何写操作 |
| 技术方案附件找不到 | 技术方案文件缺失；G2-M01 不满足，直接退回补充 |
| G1 结论找不到或 PRD 版本不一致 | G2-M01 不满足；先补 G1 或升版 PRD，不要继续准出 |
| 评论里出现两条 G2 评论 | 只应发生在历史遗留数据上；以最新一条为准继续更新，不要删评论 |
| 门禁字段不存在 | 返回 `{skipped:true}`：照常交付评论，对话里说明"该 Jira 尚未配置 G2 门禁字段" |
| 工作区清理失败 | 检查 `--key` 是否正确、目录是否在 `work/jira-gate-2` 下；路径不对时脚本会拒绝执行 |
