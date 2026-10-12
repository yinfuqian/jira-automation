# Jira 只读取数细节（jira-gate-1-mock）

面向 `jira-gate-1-mock` skill 的实现说明。站点为 Jira 10.3.9 Server / Data Center（生产站点名"Jira-追一研发管理系统"）。

**本技能对 Jira 只读**：本文档只保留取数、格式与排查相关内容；写评论、回写门禁字段、上传附件与状态流转的实操见 `jira-gate-1` 的 `references/jira-api.md`。本技能的交付物是压缩包（评论草稿 + 评审报告），不在 Jira 上留痕。

**基础地址不要写死**，`jira.mjs` 按以下优先级解析 `baseUrl`：

1. 函数入参 `options.baseUrl` / 命令行 `--base-url`
2. 环境变量 `JIRA_BASE_URL`
3. 令牌文件里的 `baseUrl`（仅 JSON 配置支持）
4. 内置默认值 `DEFAULT_BASE_URL`（生产 Jira）

容器环境由 `.env` 注入 `JIRA_BASE_URL`（例如测试站），优先级高于令牌文件，因此只要该变量存在就不会误连生产。
`selftest()` 返回的 `baseUrlSource` 字段会标明本次地址来自哪一级，**执行前先看它**，确认打在预期的站点上。

## 1. 为什么取数必须走 node_repl

- Codex 的 PowerShell 沙箱无法建立 TLS 连接（报 `安全包中没有可用的凭证` / `SSL connection could not be established`），即便放通网络权限也一样，因此**不能用 `Invoke-WebRequest` / `curl` 访问 Jira**。
- `node_repl` 的 Node 进程不受该限制，`fetch` 可正常访问 Jira。
- `node_repl` 内的约束：没有 `process`、不支持静态 `import`、`eval` 被禁用。因此 skill 脚本一律用 `await import()` 动态加载。
  环境变量经 `readEnv()` 探测（无 `process` 时安全返回空值），所以同一份脚本在桌面端与容器里都能用：桌面端读不到环境变量就回退令牌文件，容器里则由 `JIRA_TOKEN` / `JIRA_BASE_URL` 注入。
- `node_repl` 会缓存已导入模块；改了脚本后用 `import("file:///...jira.mjs?v=时间戳")` 强制重新加载。

## 2. 认证

### 2.1 个人访问令牌（首选）

创建：Jira → 右上角头像 → 个人访问令牌 → 创建令牌。请求头：`Authorization: Bearer <token>`。

凭证解析优先级：容器内由后端注入 `SKILLRUN_ENV_LOCKED=1` 时**只认环境变量** `JIRA_TOKEN` / `JIRA_BASE_URL`，不接受入参、令牌文件与其它站点；桌面端未注入该标记时按「入参 `token` / 命令行 `--token` → 环境变量 `JIRA_TOKEN` → 令牌文件」解析。

令牌文件的查找顺序（路径在 Windows 与 Linux 下都适用）：

1. `<skill 目录>/.jira-token`、`.jira-token.txt`、`jira-token.txt`
2. `<skill 目录>/../` 下的同名文件
3. `~/.codex/jira-token.txt`
4. `~/.codex/jira/token.txt`
5. `~/.codex/jira/config.json`，内容形如 `{"token":"...","baseUrl":"<按环境填写>"}`

其中 3、4、5 对应「skill 目录往上两级」，因此技能装在 `~/.codex/skills/<skill_id>/` 时可直接命中。

文件内容为单行令牌即可，也支持 `JIRA_PAT=xxx` 形式。脚本按行读取第一行非空内容，不会回显令牌。

### 2.2 匿名读取的边界

公开项目的单子可以不认证读取（实测 `CALL-1446`、`CALL-1178` 返回 200）；非公开项目（如 `ZY20260061-*`）匿名读取返回 `HTTP 401 您没有查看特定问题的权限；必须登录`。本技能只读，不存在"写入必须认证"的问题，但非公开项目仍需要令牌才能读取。

## 3. 常用只读接口

| 用途 | 方法与路径 |
|---|---|
| 连通性与版本 | `GET /rest/api/2/serverInfo`（无需认证） |
| 当前账号（校验令牌） | `GET /rest/api/2/myself` |
| 需求单全量字段 | `GET /rest/api/2/issue/{KEY}?expand=names,renderedFields,schema` |
| 当前可用的工作流流转（只读，用于说明"若真实执行会怎样"） | `GET /rest/api/2/issue/{KEY}/transitions` |
| 按父单查子任务 | `GET /rest/api/2/search?jql=parent%20%3D%20{KEY}` |
| 全部评论（超过一页时） | `GET /rest/api/2/issue/{KEY}/comment` |
| 门禁字段定义 | `GET /rest/api/2/field`（按字段名匹配 `G1准入结论` 等） |
| 门禁字段当前值（只读，用于给出预演值） | `GET /rest/api/2/issue/{KEY}?fields=<字段id列表>` |
| 附件下载 | `GET {attachment.content}`，需带同一认证头 |

固定请求头只有 `Accept: application/json`：本技能的 `request(apiPath, options)` **方法固定为 GET、不接受请求体**，因此没有 `POST` / `PUT` / `DELETE` 入口，也不带写请求才需要的 `X-Atlassian-Token`。要写 Jira 请改用 `jira-gate-1`。

Jira 错误响应形如 `{"errorMessages":["..."],"errors":{...}}`，`jira.mjs` 会把它拼成可读错误信息。

## 4. 字段映射

`expand=names` 会返回 `names` 映射（`customfield_xxx` → 中文名），`jira.mjs` 用它把自定义字段翻译成中文键，例如：

- `产品归属`、`客户名称`、`需求实现类型`、`问题反馈来源`、`是否首次`、`批次编号`、`问题发现产品`、`项目阶段`、`Story Points`、`Rank`（噪音，渲染时跳过）、`Development`（噪音）。

需求单常见标准字段：`issuetype`、`summary`、`description`、`status`、`priority`、`resolution`、`reporter`、`assignee`、`labels`、`components`、`versions`（影响的版本）、`fixVersions`（修复的版本）、`issuelinks`（问题链接）、`subtasks`（子任务，注意是复数）、`attachment`、`comment`、`timetracking` / `timeoriginalestimate`（工时）。

工时要注意：`timeoriginalestimate` 为空表示**没有评估工时**，是 checklist §6.7.1 的直接证据（但该条不阻断 G1）。

## 5. Jira wiki 标记对照（写评论草稿用）

评论草稿要用 Jira wiki 标记，用户才能直接粘贴进需求单评论区：

| 需要表达 | 写法 |
|---|---|
| 三级 / 四级标题 | `h3. 标题` / `h4. 标题` |
| 粗体 | `*文字*` |
| 无序列表 | 行首 `* ` |
| 有序列表 | 行首 `# ` |
| 绿勾（检查项通过） | `(/)` |
| 红叉（检查项不通过） | `(x)` |
| 表格 | 表头 `||A||B||`，数据行 `|a|b|` |
| 等宽 | `{{文字}}` |
| 引用 | 行首 `bq. ` |
| 单元格内不换行 | 单元格内容写成一行，避免表格错位 |

补充：

- 草稿只写到本地文件 `<工作目录>/work/jira-gate/<KEY>/comment.txt`，**不会提交到 Jira**；是否粘贴由用户决定。
- 单元格里出现 `|` 需转义为 `\|`，否则表格列会错位；正文中避免未转义的花括号组合（`{color}` 之类会被 Jira 当作宏）。
- 不要提交 Markdown 语法（`**粗体**`、`# 标题`、单竖线表格）：`pack-deliverables.mjs` 会就此给出 warning。

## 6. 浏览器兜底通道的操作要点

1. `cua.createBrowserTab("iab", "<baseUrl>/browse/KEY", { visible: false })` 打开单子；`<baseUrl>` 取上述解析结果（容器内即 `JIRA_BASE_URL`），**不要写死生产域名**。
2. `await tab.playwright.evaluate(() => document.body.innerText)` 取正文——描述、详情字段、附件列表、问题链接、子任务、评论区都在其中。
3. 附件下载：定位附件链接后触发下载，再到下载目录跑 `extract.py`（压缩包会自动解压，见 §7 第 6 条）。
4. **不要在页面里提交评论**：本技能只读，评论内容只写进草稿文件。
5. 浏览器里访问 `/rest/...` 会被拦截（`net::ERR_BLOCKED_BY_CLIENT`），只能读页面。

## 7. 实测踩过的坑（已修复，改脚本时不要回退）

1. **子任务字段名是 `subtasks`（复数），不是 `subtask`**。写错时 `fields.subtask` 为 `undefined`，会把有 6 个子任务的单子误判成"未拆解子任务"，直接误伤 checklist §6.7.1。
   实测 `ZY20260061-3343`：`fields=subtask` 返回空，`fields=subtasks` 返回 6 条。
   双保险：`getIssue()` 在 `subtasks` 为空时再用 JQL `parent = {KEY}` 查一次（见 `fetchSubtasksByParent()`）。
2. **`extract.py` 必须先把 stdout 改成 UTF-8**。Windows 控制台默认 GBK，附件里出现 emoji（例如 tts 接口文档里的 🌐）时 `print` 会抛 `UnicodeEncodeError`，而且异常发生在写 `--out` 之前，会连带丢掉输出文件。脚本里已用 `force_utf8_stdout()` 处理。
3. **描述里的 Jira wiki 标记要清理**。`{color:#de350b}…{color}`、`[^附件名]`、`{code}` 会污染描述文本；`cleanWikiMarkup()` 负责清理，但保留 `||表头||` 这类有信息量的表格语法。
4. **浏览器访问 `/rest/...` 会被拦截**（`net::ERR_BLOCKED_BY_CLIENT`），浏览器通道只能读页面，不能直接调 API。
5. **本技能不做权限预检**：读权限不足时 Jira 直接返回 401/403，按错误如实说明即可；`mypermissions` 是写操作前的事，`jira-gate-1` 才需要。
6. **压缩包附件必须"先解压再解析"**。需求单常把 PRD / 原型 / Overview 打包成 `.rar` 或 `.zip`，直接解析压缩包只会得到乱码或"无法解析"。`extract.py` 会自动识别 `.zip / .rar / .7z / .tar / .tar.gz / .tgz / .gz / .bz2 / .xz`，解压到附件目录下的 `__unpacked\` 后递归解析，输出用 `压缩包名/内部路径` 标注来源。
   `.rar` 没有可用的纯 Python 实现，因此按系统命令降级：`bsdtar`（libarchive，实测支持 RAR5）→ `7z` → `7zz` → `7za` → `unar` → `unrar` → `tar`（Windows 桌面端的 bsdtar 命令名就是 `tar`）。服务端镜像已预装 `bsdtar` 与 7-Zip 系命令，**不需要也不允许在任务中联网安装**。实测 CALL-1941 的 `.rar` 三件套（PRD_DELIVERY / Prototype / Overview）可正常解出，中文目录名正常。
   解压失败时输出 `[压缩包解压失败]` 并列出尝试过的命令与报错，**此时必须记为"附件无法解析"，不能当成"附件没有内容"，也不要臆测包内内容**。
   同一目录内的 `__unpacked` 在遍历时会被跳过，不会重复解析；同名解压结果自动加 `-2` 后缀，不覆盖上一次结果。
7. **ZIP 打包自己实现，不调系统命令**。`pack-deliverables.mjs` 用 `node:zlib` 手写 ZIP（deflate + CRC-32 + UTF-8 文件名标志位），因为容器里不一定有 `zip`、Windows 上 `Compress-Archive` 也未必要得到；中文文件名靠 `0x0800` 标志位保证在 Windows 资源管理器里正常显示。

## 8. 故障排查

| 现象 | 原因与处理 |
|---|---|
| `SSL connection could not be established` | 在 PowerShell 里访问 Jira 了；改到 `node_repl` 执行 |
| `process is not defined` | 脚本里出现了静态 `import` 或 `process.xxx`；改为 `await import()`，令牌从文件读 |
| `HTTP 401 必须登录` | 令牌缺失/失效，或访问了非公开项目；先 `selftest()`，必要时用浏览器通道 |
| 附件下载 404 | 单子里的附件已被删除；按"附件缺失"记录，不要静默跳过 |
| 输出里有 `[压缩包解压失败]` | 运行环境缺少可用解压命令（镜像异常或换了非容器环境）；如实记为"附件无法解析"并报告，不要自行安装软件，也不要臆测包内内容 |
| 评论数与 `fields.comment.total` 不一致 | `getIssue()` 会自动再拉一次 `/comment` 全量接口 |
| 找不到 `addComment` / `transitionToStatus` / `uploadAttachment` / `setGateResult` | 本技能是只读预演版，这些写函数刻意不存在；要写 Jira 请用 `jira-gate-1` |
| `JIRA_PAT` 缺失导致 review-jira-songlizhi 直接退出 | 忘了凭据桥接；见 `SKILL.md` §10 的 `export JIRA_PAT="$JIRA_TOKEN"` |
| `report-conclusion.mjs` 报「匹配到 N 条结论」 | 报告里有多个 issue；必须带上 `<KEY>` 参数后重跑 |
| `report-conclusion.mjs` 报「报告里找不到 XXX 的小节」 | 报告不是这份 Key 的（常见于复用了上一轮的报告路径）；用本轮 `report.mjs` 刚输出的那个 md 路径 |
| `report-conclusion.mjs` 报「结论取值非法」 | 报告结构变了（非「- 结论：**X**」格式）；核对报告版本，不要手工塞一个结论进草稿 |
| `[打包失败]（file-not-found）` | 评论草稿或报告路径写错；核对路径后重跑，不要重试不存在的文件 |
| `[打包失败]（key-invalid）` | 单号格式不合法（需形如 `CALL-1941`）；检查入参，不要用 URL 或路径当单号 |
| `[打包失败]（missing-argument）` | 少了 `--key` / `--comment` / `--report`；按脚本头部注释的用法补齐 |
| 打包输出里 `warnings` 非空 | 评论草稿残留 Markdown 语法或缺检查项标记，或设计报告里还有 `{{...}}` 占位符；按 `SKILL.md` §7、§8、§11 修好再重新打包 |
| 报告里出现 `{{需求名称}}` 之类的原文 | `review-jira-design` 的模板占位符没替换完；回到它的 `SKILL.md` §7 重新生成，不要手工改模板 |
| `review-jira-design` 截图脚本报找不到 Playwright / 浏览器 | 容器内通常没有浏览器；按它的「无法截取时」记为"截图未验证"，用 HTML 路径 + selector 定位，**不要安装浏览器、不要伪造截图** |
| 设计报告缺失或落在技能目录里 | 报告必须写到 `<工作目录>/work/jira-gate/<KEY>/design/`（技能目录是只读挂载）；按 `SKILL.md` §11 的路径重跑 |
| 压缩包里的中文文件名乱码 | 解压工具过旧；`pack-deliverables.mjs` 已置 UTF-8 标志位，建议用系统资源管理器或 `Expand-Archive` / `bsdtar` 解压 |
| 单子上看不到评论/附件（预期如此） | 本技能不写 Jira：评论只在压缩包内的 `comment.txt`，报告只在压缩包内；要留痕请改用 `jira-gate-1` |
