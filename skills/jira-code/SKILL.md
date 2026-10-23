---
name: jira-code
description: 读取 Jira 单上的「开发方案」附件，确定 GitLab 仓库与基线分支，从基线分支拉出 feature/<KEY> 分支并把方案实现成代码、提交并推送到远端；全程只用一条自动刷新的 Jira 评论汇报进度与异常。当用户要求「按开发方案实现这个需求」「照方案写代码并推分支」「jira-code 跑一下这张单」时使用。
---

# jira-code：按开发方案实现代码

把一张 Jira 需求单上的「开发方案」附件读成代码改动：找到方案里写的 GitLab 仓库与基线分支 → 拉出 `feature/<KEY>` 分支 → 按方案写代码 → 提交并推送 → 再用方案里写的流水线地址把本次改动**热更新**到对应环境。整个过程**不等人工确认**，并在 Jira 单上维护**唯一一条**进度评论，每 5 分钟自动刷新。

## 1. 能力与硬边界

**本技能做**：读单子与开发方案、在 feature 分支上写代码、提交、推送、按方案里的流水线地址热更新到目标环境、用一条评论汇报进度与异常。

**本技能不做**（做了就是错误执行）：

- 不改单子状态、不流转工作流（`transition` 相关能力刻意没有开放）。
- 不写自定义字段（门禁字段等一律不碰）。
- 不上传附件、不改单子标题与描述。
- 不动基线分支（基线分支只读）：不向 `main`/`master`/`release/*` 等基线分支直接提交或强制推送。
- 不新增第二条进度评论：一条需求单只允许一条进度评论（详见 §4）。
- 不臆造方案里没有的仓库、分支、需求内容，也不臆造流水线地址：热更新用的地址只能来自 `plan.mjs` 的提取结果。
- 热更新只影响容器内的制品文件，**不走 CI 编译、不建镜像、不流转单据状态**。

**交付物**是远端仓库里的 `feature/<KEY>` 分支、Jira 单上的那一条评论，以及（方案提供了流水线地址时）目标环境上的热更新结果。本地文件都只是中间产物：
推送完成、热更新处理完之后，必须用 `git-flow.mjs cleanup` 把刚拉下来的检出目录删掉，**不在工作区留代码副本**
（只对推送成功的目录做清理，没推上去的目录要保留下来便于排查；清理顺序见 §3.9）。

## 2. 运行环境与凭据

运行环境是服务器容器：没有桌面工具，所有操作都用 shell 执行本技能自带脚本（`node <技能目录>/scripts/*.mjs`）。

- 技能目录（容器内只读）：`/data/skills/jira-code`；下文用 `<技能目录>` 代指。
- 技能根目录（容器内只读）：`/data/skills`，本技能是它的一个子目录。热更新要参照的兄弟技能
  `devops-mcp-invoker` 同样在技能根目录下（`/data/skills/devops-mcp-invoker`），可以按
  `<技能根目录>/devops-mcp-invoker/SKILL.md` 与 `<技能根目录>/devops-mcp-invoker/references/REFERENCES-HOTRELOAD.md` 读取；
  根目录不是 `/data/skills` 时先用 `ls <技能目录>/..` 或
  `find / -maxdepth 6 -name "REFERENCES-HOTRELOAD.md" 2>/dev/null` 定位，不要因此跳过热更新。
- 工作区：进程当前目录（技能任务的工作目录），代码检出与状态文件都放这里，不要写进技能目录。

| 环境变量 | 用途 | 缺失时的后果 |
|---|---|---|
| `JIRA_TOKEN` | Jira 个人访问令牌，`jira-cli.mjs` 读取（服务端注入后锁定，不读令牌文件） | 服务端缺该配置时任务直接失败，不会回落到令牌文件 |
| `JIRA_BASE_URL` | Jira 站点地址；技能只允许访问该站点，传入其它站点的链接会被拒绝 | 服务端缺该配置时任务直接失败 |
| `GITLAB_PRIVATE_TOKEN` | GitLab 访问令牌（推送必需，需要写仓库权限） | 只能拉取，推送会失败 |
| `GIT_USER` / `GIT_PASSWORD` | 上没有令牌时的账号密码兜底 | — |
| `GIT_BASE_URL` | GitLab 站点地址，方案里只写「group/repo」时用来补主机 | 需要显式传 `--base-url` |
| `DEVOPS_MCP_TOKEN` | 流水线平台 API Token（热更新必需），由后端按 `.env` 写进 `CODEX_HOME/config.toml` 的 MCP 配置（同名旧配置会被覆盖） | 没有 MCP 工具可用，热更新阶段只能跳过 |
| `DEVOPS_MCP_URL` | 流水线 MCP server 地址 | 回落到内置默认地址 |

先做一次自检，把凭据状态确认清楚再动手：

```bash
node <技能目录>/scripts/jira-cli.mjs selftest
node <技能目录>/scripts/git-flow.mjs creds      # 只回显凭据来源，不会打印令牌本身
```

凭据口径：令牌只作为本次 git 命令的临时参数传入，**不会写进 `.git/config`，也不会出现在任何输出里**；不要自己拼 `https://user:token@...` 去 clone，也不要让 git 弹出交互式登录（脚本已禁用交互提示）。

## 3. 标准执行流程

**顺序固定，中间不要停下来等人工确认**；只有 §3.4 的「信息不足」分支才允许中断流程。

### 3.1 读取单子

```bash
node <技能目录>/scripts/jira-cli.mjs get <KEY|URL>
```

`<KEY>` 就是单号（URL `/browse/` 后面那一段，例如 `ZYSQ-95`），也是分支名与评论里的标识。拿到单号后接着读方案（§3.2）、
提取仓库与分支（§3.3），然后按 §3.5 启动进度心跳——`start` 会立刻发布第一条评论，让 Jira 上尽早看到「开始处理」。

**不要把心跳抢到 §3.5 之前启动**：`start` 要带 `--repo`/`--branch`，而且它按「心跳进程是否还活着」决定要不要再拉一个后台进程，
提前启动会让同一张单跑出多个心跳。唯一的例外是准备按 §3.4 中断、而此前从未启动过心跳——那时先补一次 `start`（见 §3.4）。

### 3.2 下载并解析开发方案附件

```bash
# 下载附件到工作区
node <技能目录>/scripts/jira-cli.mjs attachments <KEY|URL> work/jira-code/<KEY>/attachments
# 找出「开发方案」文件（按文件名关键词匹配）
node <技能目录>/scripts/plan.mjs find work/jira-code/<KEY>/attachments
# 转成纯文本（压缩包会自动解压；docx/pdf/xlsx 都能解析）
python <技能目录>/scripts/extract.py work/jira-code/<KEY>/attachments --out work/jira-code/<KEY>/plan.txt
```

`extract.py` 优先使用容器内已装的解压工具（bsdtar / 7z / unzip）。**不要**自行 `apt-get`、`pip`、`npm` 安装工具；解压或解析失败时如实记为「方案无法解析」并按 §3.4 处理。

### 3.3 提取仓库与基线分支

```bash
node <技能目录>/scripts/plan.mjs candidates work/jira-code/<KEY>/plan.txt
```

输出 JSON：`repos`（仓库候选）、`branches`（分支候选）、`pairs`（方案里写成一组的「仓库 + 分支」）。选取规则，按优先级：

1. **优先用 `pairs`**：方案里成对写了仓库与分支时，逐对处理（方案可能涉及多个仓库，那就每个仓库各建一条同名的 `feature/<KEY>` 分支）。
2. `pairs` 为空时，只有在 `repos` 与 `branches` **各自只有一个候选**的情况下才能配对。
3. 其余情况（多仓库多分支且没有明确对应关系）→ 按 §3.4 中断，并在评论里列出候选清单请人确认。

`candidates` 的输出里还带 `pipelines`（方案里写的流水线环境地址），供 §3.8 热更新使用；没有就代表方案没给，按 §3.8 跳过热更新。

候选里带 `confidence: "high"` 的优先；`GIT_BASE_URL` 存在时，方案里只写 `group/repo` 的路径写法可以直接用。
**只允许从 `plan.mjs` 的输出里挑**，不许自己拼仓库地址或分支名。

### 3.4 信息不足：评论 + 中断

出现下列任意一种情况，就**不再往下走**（不建分支、不写代码、不推送）：

- `plan.mjs find` 失败：单子上没有开发方案附件（退出码 1）。
- `plan.mjs candidates` 失败：方案里没有仓库信息或没有分支信息（退出码 1）。
- 仓库与分支无法配对（§3.3 第 3 条）。

处理方式：把原因写进那条进度评论并置为「已中断」，然后结束任务。

**前置**：`fail` 依赖 §3.5 写下的状态文件——文件不存在时它只会报「状态文件不存在」并以退出码 1 结束，
评论根本写不出去。已经按 §3.5 启动过心跳时直接 `fail`；若你还没启动过（例如 §3.3 就发现方案缺信息），
**先补一次 `start` 再中断**（此时还不知道仓库与分支，用最小参数就行）：

```bash
node <技能目录>/scripts/progress.mjs start --issue <KEY> \
  --state work/jira-code/<KEY>/progress.json --stage "解析开发方案"
node <技能目录>/scripts/progress.mjs fail --state work/jira-code/<KEY>/progress.json \
  --message "未在开发方案里找到仓库与分支信息，已中断：<具体缺什么>"
```

中断说明要写清楚**缺什么、需要谁补什么**（例如「方案只有实现要点，没有写仓库与目标分支，请补充后重新触发」）。这类中断**不要**新建评论，也不要重复触发第二次。

### 3.5 启动进度心跳

```bash
node <技能目录>/scripts/progress.mjs start --issue <KEY|URL> \
  --state work/jira-code/<KEY>/progress.json \
  --stage "已读取开发方案" --summary "<单子标题>" \
  --repo work/jira-code/<KEY>/<仓库目录名> --repo-label "<group/repo>" --branch feature/<KEY>
```

`start` 会立刻发布一次评论，并 fork 一个后台心跳进程，默认**每 5 分钟**刷新一次（`--interval` 可调，仅自测用）。心跳会自己带上代码改动快照（分支、提交数、未提交文件数），所以即使 agent 在长时间写代码，Jira 上的进度也会往前推进。

### 3.6 拉取 feature 分支

```bash
node <技能目录>/scripts/git-flow.mjs prepare \
  --repo "<方案里的仓库>" --base "<方案里的基线分支>" --key <KEY> \
  --dir work/jira-code/<KEY>/<仓库目录名> [--base-url "$GIT_BASE_URL"]
```

分支命名是硬性规则：**`feature/<KEY>`**（KEY 为单号，例如 `feature/ZYSQ-95`），由脚本统一生成，不要自己改名、不要加日期或人名后缀。脚本会：初始化检出目录（保留干净 origin）→ 拉取基线分支 → 基于它创建/重置 `feature/<KEY>` → 配好提交身份。

基线分支不存在或拉取失败时，脚本会退出码 1 并列出远端现有分支；此时**不要**擅自换一个分支继续，按 §3.9 记为异常中断。

### 3.7 写代码、提交、推送

按方案实现代码（见 §5 编码要求），然后：

```bash
node <技能目录>/scripts/git-flow.mjs status --dir work/jira-code/<KEY>/<仓库目录名>
node <技能目录>/scripts/git-flow.mjs commit --dir work/jira-code/<KEY>/<仓库目录名> \
  --message "feat(<KEY>): <一句话说明>"
node <技能目录>/scripts/git-flow.mjs push --dir work/jira-code/<KEY>/<仓库目录名>
```

推送使用令牌作为临时参数，默认普通 push；**禁止 `--force`**，只有在需要覆盖自己刚推上去的分支时才允许 `--force-with-lease`。

多个仓库时，对每个仓库重复 §3.6–§3.7（`--dir` 换成各自目录）。

推送完成后**先别删检出目录**：如果方案给了流水线地址，热更新（§3.8）要用这个目录里的部署文件
（`docker-compose.yml` / `deployments/kubernetes/*.yaml`）和刚构建出来的制品，删了就没得传。
清理统一放在 §3.9，等热更新处理完再做。

### 3.8 热更新到流水线环境

**前置**：代码已推送成功（§3.7），且**检出目录仍然保留**（热更新要读里面的部署文件，并上传里面的构建产物）。

从 §3.3 的 `pipelines` 里拿到流水线环境地址（也可单独再跑一次 `plan.mjs pipeline <方案文本>`），
然后按 `devops-mcp-invoker` 技能的热更新流程执行。**先完整读该技能的 `SKILL.md` 与
`references/REFERENCES-HOTRELOAD.md`**，并按本技能 `references/hotreload.md` 的口径处理地址来源与降级：

注意：那个技能是给交互式会话写的，注册 MCP 后要“重启会话”。本技能跑在无头 `codex exec` 里，MCP 配置由
后端在执行前写进 `CODEX_HOME/config.toml`，**不需要注册、也没有会话可重启**——不要执行 `codex mcp add`，
不要手工改配置文件，直接用工具探测结果决定继续还是跳过。

```bash
# 1) 确认方案里的流水线地址（缺 env 的写法会被判为无效）
node <技能目录>/scripts/plan.mjs pipeline work/jira-code/<KEY>/plan.txt
# 2) 之后按 devops-mcp-invoker 的流程：探测 MCP 工具 → 上传制品 → 下载热更新工具
#    → enable_hotreload → trigger_container_reload → create_hotreload_update_record
```

硬性口径（完整清单见 `references/hotreload.md`）：

- **地址只能来自方案提取结果**，不许自己拼项目 id 或环境 id；方案给了多个环境且无法判断时，记录并跳过。
- **制品要在检出目录里现做**：方案指定了制品路径就按方案；没写就按仓库技术栈构建（`mvn -o package` → `target/*.jar`、
  `npm run build` → `dist/` 等），并如实说明用的是哪个路径。上传目录时脚本会打成 `hot-upgrade.tar`，
  **必须整个目录完整上传**，不能只挑其中几个文件。
- **上传用本地二进制 `devops-hotreload-uploader`**，不要用 MCP tool 上传；容器是 Linux amd64。
- `env_type = UAT`、`platform_type = binary` 一律拒绝热更新。
- **一个容器/Pod 只能热更新一个模块**：方案涉及多个模块就分别触发，不要合并成一次。
- **模块匹配到父组件**：环境里没有子模块、只有父组件时，**只有一个父组件候选就直接做**——
  上传仍用子模块名，更新记录挂在父组件下，并在评论与更新记录 `description` 里注明是父组件；
  有多个候选或一个都没有时才列候选并跳过（无人值守，不猜）。
- **前提是模块正常运行**：Pod 反复重启、Deployment 不存在、容器状态异常时先记录并跳过，热更新大概率也会失败。
- **模块根目录就是检出目录**：`find_images_from_deployments` 要读模块根目录下的 `docker-compose.yml` 或
  `deployments/kubernetes/*.yaml`；检出目录名不一定等于方案里的模块名，模块名以环境接口的匹配结果为准，
  不要因为名字不同就当成 `module-root-not-found` 放弃。
- 方案里写了环境变量变更就用 `--env KEY=VALUE` / `--unset-env KEY`（或 `ALL`）透传；**方案没写就不要设置或取消任何变量**。
- 强制顺序：上传 → `download_hotreload_tool_from_nexus` → `enable_hotreload` → `trigger_container_reload`
  → `create_hotreload_update_record`（即使已开启热加载，下载工具这步也不能省）。
- 热更新是**推送之后的附加阶段**：方案没给地址、缺凭据、环境不支持、制品缺失、上传失败等任何情况，
  **都不回滚已推送的代码、也不把任务判成失败**，但**结论必须显式上报**（见下面的 `--hotreload`），
  不要只在对话里说，也不要把评论写成一片「已完成」。
- **热更新成功后要在评论里写明怎么关掉热加载**（这一步必须做，否则环境会一直挂在热加载状态）：
  PaaS 环境在流水线里用当前版本重新更新/部署一次；K8S 环境要点流水线界面的「重构」，普通重新部署不会移除热加载配置。

热更新前后各刷一次进度评论（`progress.mjs update --stage "热更新" --step "..."`），仍然只更新那一条评论。

**热更新结论是强制上报项**：评论里固定渲染一行「热更新」，取值只有 `待执行` / `已完成` / `未执行` / `失败`，
脚本不猜、也不从你写的文字里提取，只看你传的 `--hotreload`：

```bash
# 成功：走完上传 → download_hotreload_tool_from_nexus → enable_hotreload → trigger_container_reload
#      → create_hotreload_update_record 之后
node <技能目录>/scripts/progress.mjs update --state work/jira-code/<KEY>/progress.json \
  --hotreload succeeded --hotreload-reason "环境 <env>、模块 <module> 已触发容器重载并登记更新记录"

# 跳过：方案没给地址、缺凭据、环境不支持、制品缺失等
node <技能目录>/scripts/progress.mjs update --state work/jira-code/<KEY>/progress.json \
  --hotreload skipped --hotreload-reason "未配置 DEVOPS_MCP_TOKEN，已跳过热更新"

# 失败：例如 enable_hotreload 或容器重载报错
node <技能目录>/scripts/progress.mjs update --state work/jira-code/<KEY>/progress.json \
  --hotreload failed --hotreload-reason "enable_hotreload 返回 500"
```

`--hotreload` 只接受 `pending` / `succeeded` / `skipped` / `failed`（`成功`、`跳过`、`失败` 等中文写法也认，
写别的脚本会直接报错退出）。**热更新阶段结束（成功、跳过或失败）时必须上报一次**；一直不上报就 `finish` / `fail`，
脚本会兜底把结论写成「未执行（未进入热更新阶段）」——兜底只是为了不漏报，不能当成可以不上报。

### 3.8.1 写机器可读结果（收尾前必做）

本技能是平台 `/skill/run` 的顶层技能，必须在清理检出目录和写最终进度评论之前，把最终结果写到**任务根目录**（当前工作目录）的 `.skill-result.json`：

```json
{
  "schemaVersion": 1,
  "skillId": "jira-code",
  "issueKey": "ZY20260061-4044",
  "conclusion": "已完成",
  "summary": "feature/ZY20260061-4044 已推送，热更新已完成，本地检出目录已清理",
  "blockingCount": 0
}
```

- `conclusion` 按实际结果写：代码已推送且交付完整写 `已完成`；代码已推送但热更新失败或缺少部分交付写 `部分完成`；没有推送或无法继续写 `失败`。
- `summary` 写清分支、提交或推送结果、热更新结论，不超过 500 字；没有阻断项时 `blockingCount` 写 `0`。
- 异常中断、脚本故障等运营性失败不要伪造 `已完成`；无法形成业务结论时可以不写该文件。
- 该文件只用于调用统计，不是交付物，不写入 Jira；平台读取入库后会自动删除，不要放进 `work/jira-code/<KEY>/` 或仓库检出目录。

### 3.9 清理检出目录并收尾

热更新处理完（成功、跳过或失败都算处理完）再清理检出目录，不要让代码留在工作区：

```bash
node <技能目录>/scripts/git-flow.mjs cleanup --dir work/jira-code/<KEY>/<仓库目录名> \
  --key <KEY> --verify-remote
```

`cleanup` 只认 `prepare` 写下的标记文件（`<检出目录>.jira-code-checkout.json`），只删本技能自己拉的目录；
`--verify-remote` 会先确认远端确实有这个 feature 分支，避免代码没推上去就把本地删了。
如果有提交还没推送、或还有未提交改动，`cleanup` 会**拒绝删除**（原因 `unpushed-commits` / `uncommitted-changes`），
此时先补推送；确实要丢弃时用 `--force`，并在评论里说明丢弃了什么。

多个仓库时逐个清理；收尾前工作区里不应再有本次拉下来的仓库目录。

最后写收尾评论：

成功：`finish`（写清分支、提交、仓库、本地已清理，以及热更新的环境/模块等要点；结论本身由上面的 `--hotreload` 渲染）；
异常：`fail`（写清失败环节与原因）。**两者都必须调用**，否则心跳会一直留在后台。
异常中断时**不要**删检出目录，保留现场便于排查。

```bash
node <技能目录>/scripts/progress.mjs finish --state work/jira-code/<KEY>/progress.json \
  --message "已推送 feature/<KEY>：<仓库> <提交摘要>"
# 或
node <技能目录>/scripts/progress.mjs fail --state work/jira-code/<KEY>/progress.json \
  --message "推送失败（auth-failed）：GitLab 令牌缺少写权限"
```

§3.8 忘了上报时，`finish` / `fail` 也接受 `--hotreload <状态> --hotreload-reason "<原因>"`，在这里补上即可；
但不要依赖兜底，兜底结果是「未执行」，会把真实成功的热更新误报成没做。

`finish` / `fail` 的返回值里 `ok:false` 说明评论没写成功（退出码同时为 1），要在对话总结里如实说明，不要说成已完成。

## 4. 进度与异常评论规范

- **一条单子只有一条进度评论**：评论末尾带固定标记行 `jira-code:progress:<KEY>`，脚本按标记查找，命中就更新那条，命中不到才新建。重复执行本技能、心跳刷新、异常上报都只更新同一条。
- **每 5 分钟自动刷新**：由 `progress.mjs start` 拉起的心跳负责，不需要 agent 自己计时。
- **阶段变化时主动刷新**（不必等心跳）：读方案、建分支、写代码、跑测试、提交、推送、收尾这些里程碑各调一次：

```bash
node <技能目录>/scripts/progress.mjs update --state work/jira-code/<KEY>/progress.json \
  --stage "编写代码" --step "已完成 5.2 批量删除接口，正在写单测"
```

推送完成后先补一条推送明细（此时检出目录还在，还没做热更新），等热更新与清理都做完再补一条收尾明细：

```bash
node <技能目录>/scripts/progress.mjs update --state work/jira-code/<KEY>/progress.json \
  --stage "推送完成" --step "已推送 feature/<KEY>，开始构建制品并热更新"
```

- **异常写进同一条评论**：中途出错先在评论里留痕再继续修（`update --step "..."`），确认无法继续时用 `fail --message "<原因>"` 收尾；不要新开评论，也不要把错误咽下去。
- 评论内容保持简短：阶段 + 最近几条明细 + 分支 + 时间 + 异常原因即可，不要贴大段代码或完整日志。
- 评论用 Jira wiki 标记（`h5.` 标题、`*粗体*`），不要写 Markdown 表格。
- 评论里有一行「热更新」是脚本固定渲染的（取值 `待执行` / `已完成` / `未执行` / `失败`），
  只能通过 `--hotreload` / `--hotreload-reason` 控制；不要在 `--step` 或 `--message` 里重复描述热更新结论。

## 5. 编码要求

1. **按方案实现**：方案里写了什么就实现什么；方案没写清楚的地方按仓库现有代码风格与最小改动原则处理，不擅自扩大范围。
2. **中途不要停下来等确认**：不要为了「确认要不要这样写」而暂停任务，一次把代码写完。
3. **尊重现有代码**：沿用仓库的目录结构、命名与依赖，不重构无关代码，不改无关文件，不删除他人代码。
4. **必须有验证动作**：能跑构建/测试就跑（`mvn -o test`、`npm test`、`pytest` 等按仓库实际技术栈选择），跑不了就说明原因；不要提交明显编译不过的代码。
5. **提交信息**：`feat(<KEY>): 简述` / `fix(<KEY>): 简述` 形式，一次提交聚焦一件事。
6. **不提交敏感信息**：不要把令牌、密码、`.env` 里的真实值写进代码或提交。
7. **改不动就如实说**：仓库拉不下来、没有权限、方案与技术栈不匹配时，按 §3.9 用 `fail` 说明，不要编造「已完成」。

## 6. 交付物与对话总结

完成后用简洁中文总结：单号、仓库、分支（`feature/<KEY>`）、提交（短 SHA + 摘要）、推送结果、
**热更新结论**（已完成／未执行／失败，以及原因）、评论状态（评论 ID 与当前状态）、以及没做到的部分与原因。

## 7. 自检清单

- [ ] `progress.mjs start` 已执行，Jira 上只有一条带标记的进度评论。
- [ ] 分支名严格是 `feature/<KEY>`。
- [ ] 代码只改在 feature 分支上，基线分支未被改动。
- [ ] 提交信息带单号，不含敏感信息。
- [ ] `git-flow.mjs push` 返回 `ok:true`，且没有使用 `--force`。
- [ ] 方案里给了流水线地址时已按 §3.8 处理热更新；地址缺失/环境不支持/凭据缺失时已在评论里写明跳过原因。
- [ ] 热更新是在检出目录还在的时候做的（制品与部署文件都取自检出目录），没有出现「先删目录再热更新」。
- [ ] 热更新结论已用 `--hotreload` 上报（成功 / 未执行 / 失败），评论里那一行「热更新」不是兜底出来的「未执行」。
- [ ] 热更新成功后已在评论里写明怎么关闭热加载（PaaS 重新部署 / K8S 点「重构」）。
- [ ] 热更新只用了方案里的地址，没有自己拼项目 id 或环境 id。
- [ ] 热更新处理完后已用 `cleanup --verify-remote` 删除本地检出目录，工作区没有留下代码副本。
- [ ] `progress.mjs finish`（或 `fail`）已执行，状态与真实结果一致。
- [ ] 已在任务根目录写 `.skill-result.json`，结论与推送、热更新和清理的真实结果一致。
- [ ] 没有改动单子状态、没有写自定义字段、没有新增第二条进度评论。
- [ ] 没有臆造流水线地址；热更新失败或跳过时没有谎报成功。

## 8. 故障排查

| 现象 | 原因与处理 |
|---|---|
| `plan.mjs find` 报「附件目录里没有开发方案文件」 | 单子确实没上传方案，或文件名不含关键词；按 §3.4 用 `fail` 中断并列出目录内实际文件 |
| `plan.mjs candidates` 报「没有找到 GitLab 仓库信息 / 分支信息」 | 方案是自然语言描述、没写地址与分支；按 §3.4 中断，不要猜 |
| `git-flow.mjs prepare` 报 `base-branch-not-found` | 方案里的基线分支在远端不存在（可能已合并删除）；按返回的现有分支清单请人确认后中断 |
| `git-flow.mjs prepare` 报 `repo-needs-base-url` | 方案只写了 `group/repo` 且没配 `GIT_BASE_URL`；让部署方补配置或显式传 `--base-url` |
| `git-flow.mjs push` 报 `auth-failed` | 令牌缺失或没有写仓库权限；按 §3.9 中断并说明需要哪种权限 |
| `git-flow.mjs push` 报 `push-rejected` | 目标分支受保护或没有推送权限；不要改用 force，如实上报 |
| `cleanup` 报 `unpushed-commits` | 还有提交没推到远端；先补 `push`，确认远端有分支后再清理 |
| `cleanup` 报 `uncommitted-changes` | 还有代码没提交；先提交推送，或确认无用后 `--force` 并在评论里说明 |
| `cleanup` 报 `not-managed-checkout` / `dir-mismatch` | 目录不是本技能拉的（没有标记文件），不要用 `--force` 绕开，人工确认后再处理 |
| `cleanup` 报 `remote-branch-missing` | 远端没有该 feature 分支，说明代码没推上去；保留目录并如实上报 |
| `plan.mjs pipeline` 返回 `pipeline-not-found` | 方案里没写流水线环境地址；按 §3.8 跳过热更新并在评论里说明，不要自己拼地址 |
| 热更新阶段没有 MCP 工具可用 | 后端未配置 `DEVOPS_MCP_TOKEN`（未写进 `CODEX_HOME/config.toml`）；记录并跳过热更新 |
| 热更新报「找不到模块根目录 / 部署文件缺失」 | 检出目录里没有 `docker-compose.yml` 或 `deployments/kubernetes/*.yaml`；如实说明并跳过，不要编造目标环境 |
| 热更新报模块匹配到父组件 | 只有一个父组件候选就直接做（上传用子模块名、更新记录挂父组件并在评论与 description 里注明）；多个或零个候选才列出来跳过 |
| 目标环境 Pod 反复重启 / Deployment 不存在 | 环境本身不正常，热更新大概率失败；先记录并跳过，建议先解决环境问题 |
| 热更新报 `env_type = UAT` / `platform_type = binary` | 这两类环境不支持热更新，直接记录并跳过 |
| 上传目录只传了部分文件 | 目录必须**完整上传**（脚本会打包成 `hot-upgrade.tar`），不能只挑其中几个文件 |
| `devops-hotreload-uploader` 不存在 | 从 devops-mcp-invoker 文档里的 Nexus 地址下载到 `~/.devops-hotreload/bin/`；下载失败就跳过 |
| 目标环境是 UAT 或 binary | 这两类环境不支持热更新，直接记录并跳过 |
| `progress.mjs` 报「进度评论写入失败」 | Jira 不可达或令牌无评论权限；检查 `selftest` 结果，把失败写进对话总结，不要谎报成功 |
| `progress.mjs` 报「无法识别的热更新状态」 | `--hotreload` 只认 `pending`/`succeeded`/`skipped`/`failed`（及少量中文同义词）；用标准值重跑 |
| 评论里「热更新」显示兜底的「未执行（未进入热更新阶段）」 | 忘记上报：热更新阶段结束时用 `update --hotreload <状态> --hotreload-reason "<原因>"` 补一次 |
| 评论里出现两条进度评论 | 只应发生在历史遗留数据上；以带标记的最新一条为准继续更新，不要删评论 |
| 心跳进程没有停 | `finish`/`fail` 未调用，或进程被强杀；用 `progress.mjs stop --state <文件>` 手动收尾 |
