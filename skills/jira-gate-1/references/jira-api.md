# Jira 取数与写评论细节

面向 `jira-gate-1` skill 的实现说明。站点为 Jira 10.3.9 Server / Data Center（生产站点名"Jira-追一研发管理系统"）。

**基础地址不要写死**，`jira.mjs` 按以下优先级解析 `baseUrl`：

1. 函数入参 `options.baseUrl` / 命令行 `--base-url`
2. 环境变量 `JIRA_BASE_URL`
3. 令牌文件里的 `baseUrl`（仅 JSON 配置支持）
4. 内置默认值 `DEFAULT_BASE_URL`（生产 Jira）

容器环境由 `.env` 注入 `JIRA_BASE_URL`（例如测试站），优先级高于令牌文件，因此只要该变量存在就不会误连生产。
`selftest()` 返回的 `baseUrlSource` 字段会标明本次地址来自哪一级，**执行前先看它**，确认打在预期的站点上。

## 1. 为什么必须走 node_repl

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

公开项目的单子可以不认证读取（实测 `CALL-1446`、`CALL-1178` 返回 200）；非公开项目（如 `ZY20260061-*`）匿名读取返回 `HTTP 401 您没有查看特定问题的权限；必须登录`。写评论必须认证。

## 3. 常用接口

| 用途 | 方法与路径 |
|---|---|
| 连通性与版本 | `GET /rest/api/2/serverInfo`（无需认证） |
| 当前账号（校验令牌） | `GET /rest/api/2/myself` |
| 需求单全量字段 | `GET /rest/api/2/issue/{KEY}?expand=names,renderedFields,schema` |
| 当前用户对某单的权限（写评论前预检） | `GET /rest/api/2/mypermissions?issueKey={KEY}` |
| 当前可用的工作流流转（只读） | `GET /rest/api/2/issue/{KEY}/transitions` |
| 执行流转 | `POST /rest/api/2/issue/{KEY}/transitions`，Body `{"transition":{"id":"<流转id>"}}` |
| 按父单查子任务 | `GET /rest/api/2/search?jql=parent%20%3D%20{KEY}` |
| 全部评论（超过一页时） | `GET /rest/api/2/issue/{KEY}/comment` |
| 写评论 | `POST /rest/api/2/issue/{KEY}/comment`，Body `{"body":"..."}` |
| 改评论 | `PUT /rest/api/2/issue/{KEY}/comment/{id}` |
| 上传附件 | `POST /rest/api/2/issue/{KEY}/attachments`，`multipart/form-data`，字段名 `file`（可带 `filename`） |
| 附件下载 | `GET {attachment.content}`，需带同一认证头 |

固定请求头：`Accept: application/json`、`X-Atlassian-Token: no-check`。

Jira 错误响应形如 `{"errorMessages":["..."],"errors":{...}}`，`jira.mjs` 会把它拼成可读错误信息。

## 4. 字段映射

`expand=names` 会返回 `names` 映射（`customfield_xxx` → 中文名），`jira.mjs` 用它把自定义字段翻译成中文键，例如：

- `产品归属`、`客户名称`、`需求实现类型`、`问题反馈来源`、`是否首次`、`批次编号`、`问题发现产品`、`项目阶段`、`Story Points`、`Rank`（噪音，渲染时跳过）、`Development`（噪音）。

需求单常见标准字段：`issuetype`、`summary`、`description`、`status`、`priority`、`resolution`、`reporter`、`assignee`、`labels`、`components`、`versions`（影响的版本）、`fixVersions`（修复的版本）、`issuelinks`（问题链接）、`subtasks`（子任务，注意是复数）、`attachment`、`comment`、`timetracking` / `timeoriginalestimate`（工时）。

工时要注意：`timeoriginalestimate` 为空表示**没有评估工时**，是 checklist §6.7.1 的直接证据。

## 5. 浏览器兜底通道的操作要点

1. `cua.createBrowserTab("iab", "<baseUrl>/browse/KEY", { visible: false })` 打开单子；`<baseUrl>` 取上述解析结果（容器内即 `JIRA_BASE_URL`），**不要写死生产域名**。
2. `await tab.playwright.evaluate(() => document.body.innerText)` 取正文——描述、详情字段、附件列表、问题链接、子任务、评论区都在其中。
3. 附件下载：定位附件链接后触发下载，再到下载目录跑 `extract.py`（压缩包会自动解压，见 §6.6）。
4. 写评论：在评论区输入文本并提交（这是对外发布内容，写入前必须让用户确认）。
5. 浏览器里访问 `/rest/...` 会被拦截（`net::ERR_BLOCKED_BY_CLIENT`），只能读页面。

## 6. 实测踩过的坑（已修复，改脚本时不要回退）

1. **子任务字段名是 `subtasks`（复数），不是 `subtask`**。写错时 `fields.subtask` 为 `undefined`，会把有 6 个子任务的单子误判成"未拆解子任务"，直接误伤 checklist §6.7.1。
   实测 `ZY20260061-3343`：`fields=subtask` 返回空，`fields=subtasks` 返回 6 条。
   双保险：`getIssue()` 在 `subtasks` 为空时再用 JQL `parent = {KEY}` 查一次（见 `fetchSubtasksByParent()`）。
2. **`extract.py` 必须先把 stdout 改成 UTF-8**。Windows 控制台默认 GBK，附件里出现 emoji（例如 tts 接口文档里的 🌐）时 `print` 会抛 `UnicodeEncodeError`，而且异常发生在写 `--out` 之前，会连带丢掉输出文件。脚本里已用 `force_utf8_stdout()` 处理。
3. **描述里的 Jira wiki 标记要清理**。`{color:#de350b}…{color}`、`[^附件名]`、`{code}` 会污染描述文本；`cleanWikiMarkup()` 负责清理，但保留 `||表头||` 这类有信息量的表格语法。
4. **浏览器访问 `/rest/...` 会被拦截**（`net::ERR_BLOCKED_BY_CLIENT`），浏览器通道只能读页面和写评论，不能直接调 API。
5. **`mypermissions` 的 `permissions` 过滤参数在本站会被忽略**，接口会返回全量权限列表；从中读 `ADD_COMMENTS`、`EDIT_ISSUES`、`TRANSITION_ISSUES` 即可。
6. **压缩包附件必须"先解压再解析"**。需求单常把 PRD / 原型 / Overview 打包成 `.rar` 或 `.zip`，直接解析压缩包只会得到乱码或"无法解析"。`extract.py` 会自动识别 `.zip / .rar / .7z / .tar / .tar.gz / .tgz / .gz / .bz2 / .xz`，解压到附件目录下的 `__unpacked\` 后递归解析，输出用 `压缩包名/内部路径` 标注来源。
   `.rar` 没有可用的纯 Python 实现，因此按系统命令降级：`bsdtar`（libarchive，实测支持 RAR5）→ `7z` → `7zz` → `7za` → `unar` → `unrar` → `tar`（Windows 桌面端的 bsdtar 命令名就是 `tar`）。服务端镜像已预装 `bsdtar` 与 7-Zip 系命令，**不需要也不允许在任务中联网安装**。实测 CALL-1941 的 `.rar` 三件套（PRD_DELIVERY / Prototype / Overview）可正常解出，中文目录名正常。
   解压失败时输出 `[压缩包解压失败]` 并列出尝试过的命令与报错，**此时必须记为"附件无法解析"，不能当成"附件没有内容"，也不要臆测包内内容**。
   同一目录内的 `__unpacked` 在遍历时会被跳过，不会重复解析；同名解压结果自动加 `-2` 后缀，不覆盖上一次结果。

## 7. 写评论前的权限预检

写评论是有副作用的外部动作，落笔前先用一次只读接口确认权限：

```javascript
const p = await j.request("GET", `/rest/api/2/mypermissions?issueKey=${key}`);
nodeRepl.write(["ADD_COMMENTS", "EDIT_ISSUES", "TRANSITION_ISSUES"].map(n => `${n}=${p.permissions[n].havePermission}`).join(", "));
```

实测 `ZY20260061-3343`：`ADD_COMMENTS=true`、`EDIT_ISSUES=true`、`TRANSITION_ISSUES=true`。若 `ADD_COMMENTS=false`，不要反复重试，直接告知用户缺少评论权限。

`TRANSITION_ISSUES` 是退回补充时流转状态（→ 评审中）所需的权限。为 `false` 时**跳过流转动作**，评论里写「未流转（缺少流转权限）」，同样不要反复重试。结论为通过时不调用流转接口，因此不依赖该权限。

`CREATE_ATTACHMENTS` 是「上传评审报告附件」所需的权限（见 §10）。为 `false` 时跳过上传，并在对话里如实说明缺权限。

## 8. 评论写入实操

- 评论模板与 Jira wiki 标记对照见 `SKILL.md`「输出模板」节；**评论正文用 Jira wiki 标记，不要提交 Markdown 表格**，否则会显示成原始符号。
- 评论只列检查点：结论摘要 + 检查项清单；每项行首必须有标记——通过写 `(/)`（绿勾），不通过写 `(x)`（红叉）。不通过项每条一行、80 字以内；判定过程留在对话回复里。
- 不达标时元信息区多一行「待处理人」：`* 待处理人：[~登录名]｜流转：<实际流转结果>`，`@` 写法见 §9。
- 评论**末尾固定一行**「报告附件 + 评审结论」：`* 报告附件：<KEY>-需求评审报告-宋立志.md（已上传）｜评审结论：需修改`，与检查项清单之间空一行；达标与不达标都要有（见 §10 第 8 条）。`评审结论` 只取 `不合格 / 需修改 / 合格`，且只能由 `report-conclusion.mjs` 从报告 md 提取，不许自己判。
- 写入后读回验证，确认渲染正常、无截断：

```javascript
const c = await j.addComment(key, text);
const back = await j.getIssue(key);
const last = back.comments[back.comments.length - 1];
nodeRepl.write(last.id + " | " + last.author + " | " + last.body.slice(0, 120));
```

- Jira wiki 表格的单元格内容必须在一行内写完；单元格里出现 `|` 需转义为 `\|`，否则表格列会错位。
- 评论正文中避免出现未转义的花括号组合（`{color}` 之类会被当作宏）。
- 结论只以评论留痕，不生成报告文档；本地仅保留附件与解析中间文件。

## 9. 交付顺序与「仅退回补充时流转并 @ 流转人」

第 6 条的交付顺序对所有结论都适用（评论 → 门禁字段 → 附件 → 流转）。前三步对所有结论执行，**状态流转只在结论为「退回补充」时执行**：

- 结论「通过」：**不流转状态**，保持当前状态，允许进入技术方案设计；不 @ 人。
- 结论「退回补充」：流转到 **评审中**，并在评论里 @ 流转人。

**先做类型预检**：只有问题类型为「故事」（英文站点 `Story` 视为同一类型）才执行本技能；类型不符时直接停止，不做任何写操作。命令行入口：

```bash
node scripts/jira-cli.mjs precheck <KEY|URL>
# {"ok":true,"key":"...","issueType":"故事","expected":"故事","supported":true,"status":"需求设计"}
```

1. **流转人从哪来**：本站在 `/rest/api/2/field` 里没有名为「流转人」的字段（实测名字含「流转」的字段为 0 个），因此本技能已确认的口径是**取经办人 `assignee`**；仅在经办人为空时才按 `assignee → reporter → creator` 回退，回退取到时对话摘要里要写明实际来源（`resolveFlowOwner()` 返回的 `source` 字段会标明）。需要长期换口径时在技能目录的 `jira-config.json`（即仓库 `skills/jira-gate-1/jira-config.json`，随技能目录挂载进容器）里用 `flowOwnerField` 指定人员字段名，**不要改脚本**。
2. **@ 写法**：Jira Server / DC 按登录名引用，`[~liu.huan]` 渲染为「刘欢」。`resolveFlowOwner()` 直接返回拼好的 `mention`；实测 `flow-owner ZYSQ-95` → `{"name":"liu.huan","displayName":"刘欢","mention":"[~liu.huan]","field":"assignee"}`。
3. **退回补充的目标状态固定为「评审中」**；结论为通过时不得调用流转接口。**不要写死流转 id**：不同项目工作流的流转 id 与流转名都可能不同，`transitionToStatus()` 按目标状态名匹配（先比 `to`，再比 `name`）。

```bash
node scripts/jira-cli.mjs transitions <KEY|URL>             # 只读：看当前可用流转
node scripts/jira-cli.mjs flow-owner <KEY|URL>              # 只读：看流转人与 @ 写法
node scripts/jira-cli.mjs transition <KEY|URL> --to 评审中       # 仅退回补充
```

4. **匹配不到流转时不抛错**：返回 `{ok:false, reason:"no-transition", status, target, available}`，`available` 是当前可用流转列表。若当前状态没有通向目标状态的流转，按 `SKILL.md` §9 记「未流转（该单当前状态无目标状态流转）」，并在对话里列出 `available` 交人工处理，不要改用其他状态。
5. 已在目标状态时返回 `{ok:true, skipped:true, reason:"already-in-target"}`，不重复发起流转。
6. 写评论、回写门禁字段、上传附件、流转的顺序固定为：**评论（含 @）→ 门禁字段 → 附件 → 流转**，流转放最后（见 §10）。前三步对所有结论都执行；流转只在退回补充时执行，目标为「评审中」，通过不流转。
7. 因为是评论先写、流转后做，评论里的「流转：」先按预期结果写；流转跑完必须核对返回值，任一非成功分支都用 `comment-update` 改成实际措辞（措辞见 `SKILL.md` §9 的表）。

## 10. 评审报告附件上传

需求质量的细读结论由同仓库技能 `review-jira-req` 产出（对 Jira 只读），本技能负责把它的 Markdown 报告上传成需求单附件。

1. **技能位置**：容器内 `/data/skills/review-jira-req`（随 `skills/` 只读挂载）；桌面端为 `<仓库>/skills/review-jira-req`。执行它的流水线前先做凭据桥接——桌面端它读 `JIRA_PAT`，容器锁定模式下直接读 `JIRA_TOKEN`：

```bash
export SKILL_DIR=/data/skills/review-jira-req
export JIRA_PAT="$JIRA_TOKEN"
export JIRA_BASE_URL="${JIRA_BASE_URL:-https://jira.in.wezhuiyi.com}"
```

2. **流水线**：`bin/jira-fetch.mjs`（拉取，只读 GET）→ 归一化草稿 → `bin/rulecheck.mjs`（12 条规则）→ 语义评审 + `bin/verify-semantic-quotes.mjs`（引用逐字自检）→ `bin/report.mjs`（输出 `reports/review-report-<时间戳>.md`）。产物落在技能工作区，不写进只读的技能目录。
3. **附件命名**：`<KEY>-需求评审报告-宋立志.md`，**必须以「宋立志」结尾**。报告脚本自带时间戳文件名，上传时用 `--name` 改成规范名。
4. **上传**：`jira.mjs` 的 `uploadAttachment()` 用 `multipart/form-data` 提交，字段名固定为 `file`；**不要手工设置 `Content-Type`**，边界由 `fetch` 生成（`request()` 已对 `FormData` 特判）。Node 20 起 `FormData` / `Blob` 是全局对象，容器内可用。

```bash
node scripts/jira-cli.mjs attach <KEY|URL> "<报告路径>" --name "<KEY>-需求评审报告-宋立志.md"
node scripts/jira-cli.mjs attach-list <KEY|URL>   # 读回确认：列表里出现目标文件名才算上传成功
```

5. **成功判定看返回体不看退出码**：`attach` 成功返回 `{ok:true, id, filename, size}`；失败返回 `{ok:false, reason}` 并**置退出码 1** 且在 stderr 打 `[附件上传失败] ...`。上传完后**必须**用 `attach-list` 读回，确认 `fields.attachment` 里出现目标文件名——`ok:true` 只说明站点接受了这次 POST，读回才是"附件真的挂在该单上"的证据。注意附件的 `author` 是令牌账号，不是写评论的人，这一点不是异常。
6. **降级**：本地文件缺失、403（缺 `CREATE_ATTACHMENTS`）、404（该单未启用附件）、网络错误都返回 `{ok:false, reason}` 而不抛错：**跳过上传并如实说明，不要重试、不要改文件名绕开**。
7. **顺序**：评论 → 门禁字段 → 上传附件 → 流转状态（仅不达标，见 §9）。流转放最后，前序步骤的留痕不因流转失败而回滚。
8. **评论末尾固定带附件行 + 评审结论**：评论里写 `* 报告附件：<KEY>-需求评审报告-宋立志.md（已上传 / 未上传（原因））｜评审结论：<不合格/需修改/合格>`。
   - 附件名按第 3 条规则可提前确定；`评审结论`用本技能的 `scripts/report-conclusion.mjs` 从刚生成的报告 md 里提取（`node scripts/report-conclusion.mjs "<报告路径>" <KEY>`），**它是唯一来源，不许自己判**。取值只有三个，脚本遇到非法取值或找不到该 KEY 会以退出码 1 报错，此时如实说明，不要猜。
   - 为了让结论可提取，**五步流水线必须在写评论之前跑到第 5 步**；附件上传仍是写评论之后的事。
   - 上传失败时用 `comment-update` 把括号里改成「未上传（原因）」，`评审结论`保留提取到的真实取值——附件没传上不代表没有结论。

## 11. 故障排查

| 现象 | 原因与处理 |
|---|---|
| `SSL connection could not be established` | 在 PowerShell 里访问 Jira 了；改到 `node_repl` 执行 |
| `process is not defined` | 脚本里出现了静态 `import` 或 `process.xxx`；改为 `await import()`，令牌从文件读 |
| `HTTP 401 必须登录` | 令牌缺失/失效，或访问了非公开项目；先 `selftest()`，必要时用浏览器通道 |
| `HTTP 403` 且带 XSRF 提示 | 缺 `X-Atlassian-Token: no-check` 头 |
| 附件下载 404 | 单子里的附件已被删除；按"附件缺失"记录，不要静默跳过 |
| 输出里有 `[压缩包解压失败]` | 运行环境缺少可用解压命令（镜像异常或换了非容器环境）；如实记为"附件无法解析"并报告，不要自行安装软件，也不要臆测包内内容 |
| 评论数与 `fields.comment.total` 不一致 | `getIssue()` 会自动再拉一次 `/comment` 全量接口 |
| `{ok:false, reason:"no-transition"}` | 当前状态没有通向目标状态的流转；按返回的 `available` 如实记录，不要改用其他状态 |
| 流转返回 403 | 缺 `TRANSITION_ISSUES` 权限；跳过流转，评论里写「未流转（缺少流转权限）」 |
| `[~xxx]` 没有渲染成 @ 人 | 用户名写成了显示名；要用**登录名**（如 `liu.huan`），可用 `flow-owner` 直接取 `mention` |
| 上传返回 `reason:"forbidden"` | 缺 `CREATE_ATTACHMENTS` 权限；跳过上传并说明，不要重试 |
| 上传返回 `reason:"not-found"` | 该单未启用附件或单号不对；如实说明 |
| 上传返回 `reason:"file-not-found"` | 报告 md 没生成成功；回到 `report.mjs` 那一步核对路径 |
| 上传报 `Content-Type` 相关 400/415 | 手工设置了 multipart 的 `Content-Type`；改为让 `fetch` 自动生成边界 |
| 上传返回 `ok:true` 但单子上看不到附件 | 站点接受了 POST 不等于挂上了；用 `attach-list` 读回 `fields.attachment` 确认，并核对是否看的是同一站点（生产/测试的 Key 可能同名） |
| 附件作者与评论作者不是同一人 | 附件按**令牌账号**记录作者，评论按令牌账号写入，属正常现象 |
| `JIRA_PAT` 缺失导致 review-jira-req 直接退出 | 桌面端忘了设置凭据；容器锁定模式应检查 `JIRA_TOKEN`，见 §10 的凭据桥接 |
| `report-conclusion.mjs` 报「匹配到 N 条结论」 | 报告里有多个 issue；评论那行只服务单个需求，必须带上 `<KEY>` 参数后重跑 |
| `report-conclusion.mjs` 报「报告里找不到 XXX 的小节」 | 报告不是这份 Key 的（常见于复用了上一轮的报告路径）；用本轮 `report.mjs` 刚输出的那个 md 路径 |
| `report-conclusion.mjs` 报「结论取值非法」 | 报告结构变了（非「- 结论：**X**」格式）；核对报告版本，不要手工塞一个结论进评论 |

## 12. 门禁字段与工作流门禁（Jira 管理端配置）

### 12.1 为什么不能在工作流里直接调 AI

工作流校验器/条件必须是**同步、确定性、亚秒级**的；AI 预检是异步、非确定性的，还要读附件。因此正确架构是两段式：

1. **AI 预检**（本 skill）：读需求单 → 判定 → 写评论 → 用 `setGateResult()` 把结论回写到 Jira 字段。
2. **工作流门禁**（Jira 配置）：流转校验只读那个字段，不调用 AI。

### 12.2 需要 Jira 管理员做的事

用管理员账号（本 skill 的令牌**没有**管理员权限，做不了这三件事）：

1. **建自定义字段**（管理 → 问题 → 自定义字段），类型建议：

| 字段名 | 类型 | 取值 |
|---|---|---|
| `G1准入结论` | 单选列表 | `未检查` / `通过` / `不通过` / `豁免` |
| `G1预检时间` | 日期时间 | — |

  字段名可自定义，但要和 `config.json` 里的 `gateResultField` 等保持一致。

   > 本版本按 PRD 需求准入 Checklist 判定，不再产出需求难度；`G1需求难度` 字段若已存在可保留，但技能不会写入。

2. **加入屏幕**：把上述字段加到 `故事` 的**查看**和**编辑**屏幕上（REST 写字段要求字段在该问题类型的编辑屏幕上）。

3. **加流转门禁**：在人工推进的「需求设计 → 开发设计中」这条流转上二选一（本技能通过时不自动流转，由人工按门禁结果决定是否推进）：

| 方案 | 配置 | 效果 | 依赖 |
|---|---|---|---|
| A. 隐藏按钮 | 条件（Condition）→ 内置 **Value Field**，`G1准入结论 = 通过` | 不满足时按钮直接不显示 | 无，Jira 原生支持 |
| B. 拦截并报错 | 校验器（Validator）→ ScriptRunner `Simple Scripted Validator` / JMWE 条件校验 | 按钮可见，点击后报错提示去评论区看缺口 | 需要 ScriptRunner 或 JMWE |

  推荐 **B**（能告诉用户为什么不能流转，整改闭环更短）；两种都不影响 REST 与批量流转路径，因为条件和校验器在所有路径上都会执行。

### 12.3 两个必须堵的漏洞

1. **结论可被手工绕过**：任何有「编辑问题」权限的人都能把 `G1准入结论` 手动改成 `通过`。
   - 轻量缓解：字段配置里把 `G1准入结论` 设为只读，让 UI 改不了（REST 仍可写，需实测确认）。
   - 强校验：校验器里额外读该字段的改动记录，要求**最后一次修改者必须是 AI 专用账号**，否则拒绝流转。
2. **结论会过期**：AI 判完 `通过` 后，产品经理又改了 PRD 或换了附件，字段仍挂着 `通过`。
   - 缓解：用 ScriptRunner 监听器（Issue Updated），在**附件增删改或描述变更**时把 `G1准入结论` 重置为 `未检查`，强制重新预检。

### 12.4 其他运营建议

- **保留人工豁免通道**：加 `豁免` 取值，只允许产品架构师角色设置，避免 AI 误判把需求彻底卡死。
- **先观察再收紧**：建议先用方案 B 的「警告模式」跑 1~2 个迭代，统计误判率，再切成硬门禁。
- **不要写难度**：本版本不产出需求难度；如需按复杂度加严评审，请在 G2 或人工流程里单独配置。

### 12.5 相关函数

```javascript
const rf = await j.resolveGateFields();     // 探测字段是否存在：{ fields, missing, configSource }
const g  = await j.getGateResult("CALL-1940");  // 读回当前值
const r  = await j.setGateResult("CALL-1940", { result: "不通过" });
const d  = await j.setGateResult("CALL-1940", { result: "通过" }, { dryRun: true });  // 只算不写
```

不达标时的流转相关函数：

```javascript
const ts = await j.listTransitions("CALL-1446");                 // 当前可用流转 [{id, name, to}]
const fo = await j.resolveFlowOwner(issue);                      // 流转人与 @ 写法 {name, mention, field}
const back = await j.transitionToStatus("ZYSQ-95", "评审中");     // 仅退回补充时流转
```

评审报告附件：

```javascript
const up = await j.uploadAttachment("CALL-1940", "reports/review-report-20260920-101530.md", {
  name: "CALL-1940-需求评审报告-宋立志.md",
});
// 成功：{ ok:true, id, filename, size, content }
// 失败：{ ok:false, reason:"forbidden|not-found|file-not-found|network-error", status, message }
```

- `setGateResult()` 在字段不存在时返回 `{ ok:false, skipped:true, reason, hint }`，**不抛异常**，便于优雅降级。
- 单选字段写成 `{ value: "通过" }`，其他类型写字符串；`G1预检时间` 默认写入当前时间。
- 字段名覆盖方式（`~/.codex/jira/config.json`）：

```json
{
  "token": "...",
  "gateResultField": "G1准入结论",
  "gateCheckedAtField": "G1预检时间"
}
```
