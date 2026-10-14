# 流水线热更新（jira-code）

本文件说明 `jira-code` 在**代码推送完成之后**如何把本次改动热更新到流水线环境。

**权威流程不是本文**：热更新的完整约束与执行细节以同仓库技能 `devops-mcp-invoker` 为准 ——
执行前必须完整读它的 `SKILL.md` 与 `references/REFERENCES-HOTRELOAD.md`。本文只规定 jira-code 特有的部分：
**地址从哪来、什么时候做、失败怎么办**。

## 1. 地址来源：只能来自开发方案

流水线环境地址必须由 `plan.mjs` 从开发方案正文里提取，**不许自己拼、不许猜项目或环境 id**：

```bash
node <技能目录>/scripts/plan.mjs pipeline work/jira-code/<KEY>/plan.txt
```

输出：

```json
{
  "ok": true,
  "pipelines": [
    {
      "url": "https://devops.ks1.wezhuiyi.com/pipeline/project/10000054/release/20000526/env/30035160/application",
      "host": "devops.ks1.wezhuiyi.com",
      "projectId": "10000054",
      "releaseId": "20000526",
      "envId": "30035160",
      "line": 2
    }
  ]
}
```

`candidates` 子命令的输出里也带同样的 `pipelines` 字段，读方案那一步已经调用过它的话不用再跑一次。

- 只认「同一条 URL 里既有 `/pipeline/project/<id>` 又有 `/env/<id>`」的写法：缺 `env` 就无法定位环境。
- 方案里写了多个环境时**逐个处理**（每个环境各做一次热更新）；分不清哪个环境对应哪个模块时
  **记录候选并跳过**本次热更新，不要自己挑一个，也不要把任务判成失败。
- `ok:false`（`pipeline-not-found`）表示方案里没有流水线地址：此时**跳过热更新**，用
  `progress.mjs update --hotreload skipped --hotreload-reason "方案未提供流水线地址"` 上报，
  任务整体按「代码已推送、热更新未执行」收尾，**不算失败**（代码推送才是本技能的主交付物）。

## 2. 前置条件

热更新通过远端 MCP server `pipeline-integration-mcp` 完成，由后端写进 `CODEX_HOME/config.toml`：

`devops-mcp-invoker` 是面向交互式会话写的（首次注册后要“重启会话”）。**本技能是无头 `codex exec`，
每次执行都是新进程，配置已经由后端准备好，不需要也没有会话可重启**：不要执行 `codex mcp add`、
不要手工改 `config.toml`、不要提示用户重启会话。探测不到工具就按 §4 记录并跳过。

| 环境变量 | 用途 | 缺失时的后果 |
|---|---|---|
| `DEVOPS_MCP_TOKEN` | 流水线平台个人中心签发的 API Token，作为 `X-User-Tokens` 请求头 | 不注册 MCP server，热更新阶段无工具可用 |
| `DEVOPS_MCP_URL` | MCP server 地址，默认 `https://devops.ks1.wezhuiyi.com/mcp` | 用默认值 |

先确认工具是否可用：调用任一 `pipeline-integration-mcp` tool（例如 `get_environment_info`）探测。
返回 `tool not found` / `session not found` / 401 / 403 时**不要反复重试**，按 §4 记录原因并跳过。

**顺序要求（容易搞反）**：热更新必须发生在**清理检出目录之前**。`find_images_from_deployments`
要读模块根目录下的部署文件，上传的制品也是在检出目录里构建出来的；先把目录删掉就什么都传不了。
本技能的顺序是：推送（§3.7）→ **热更新（§3.8）** → 清理检出目录（§3.9）。

## 3. 执行要点（细节以 devops-mcp-invoker 为准）

1. **上传用本地二进制**：`devops-hotreload-uploader`（不是 MCP tool），放 `~/.devops-hotreload/bin/`；
   缺失时从 `http://pkg.in.wezhuiyi.com/repository/infra/devops-hotreload-uploader/latest/linux/amd64/devops-hotreload-uploader`
   下载（匿名可拉）。容器是 Linux amd64。容器里 `$HOME` 不可写导致下载失败时，把它下到工作区
   （`work/jira-code/<KEY>/bin/`）并用绝对路径调用，不要因此跳过热更新。
2. **制品来源**：本次 feature 分支上的构建产物。方案里指定了制品路径就按方案；没写就按仓库技术栈构建
   （`mvn -o package` → `target/*.jar`、`npm run build` → `dist/` 等），并如实说明用的是哪个路径。
   **没有可上传的制品就不要硬凑**，按 §4 记录并跳过。
3. **强制顺序**：上传成功 → `download_hotreload_tool_from_nexus` → `enable_hotreload` → `trigger_container_reload`
   → `create_hotreload_update_record`。即使环境已开启热加载，`download_hotreload_tool_from_nexus` 也必须调用。
4. **环境阻断**：`env_type = UAT` 与 `platform_type = binary` 一律拒绝热更新，直接记录并跳过。
5. **模块匹配**：`get_environment_info` 拿模块列表，精确匹配到模块名就直接用该模块的 `moduleId`。
   匹配不到时走**父组件回退**（环境里只有父组件 `aiforce`、要更的是子模块 `aiforce2`）。交互式流程会问人；
   本技能是无人值守的，按候选个数决定，**不猜**：
   - **只有一个**父组件候选 → **直接执行**：上传路径与文件名仍用子模块名（`aiforce2`），
     只有更新记录的 `moduleId` 用父组件，并在评论与更新记录 `description` 里**注明是挂在父组件下**的
     （见第 6 条）。
   - **多个**父组件候选 → 无法唯一确定，列出候选并**跳过**，在评论里写清「匹配到多个父组件候选 <X/Y>，
     需要人工指定后再重跑」。
   - **一个都没有** → 列出环境里可选的模块并跳过。
6. **更新记录**：`description` 必须包含实际模块名，推荐
   `热更新 <module> 模块的 <文件> 到 <target_dir> 目录（<KEY>）`。
   **走到父组件回退时**（第 5 条），`description` 里追加一句注明更新历史记在父组件下，例如
   `热更新 aiforce2 子模块的 aiforce2.jar 到 /app/bin 目录（ZYSQ-95），更新记录挂在其父组件 aiforce 下`；
   评论里也要写同一句话，让人知道这条记录为什么不在 `aiforce2` 下。
7. 令牌只用于 MCP 请求头与上传二进制，**不写进仓库、不写进评论、不打印到输出**。
8. **上传参数**：`--remote-dir` 按 `项目id/环境名-环境id/模块名/` 组织，末尾斜杠不能省。
   项目 id、环境 id 来自 `plan.mjs pipeline` 的结果，**环境名与模块名必须从 `get_environment_info`
   的返回里取**（方案里的环境名只作对照，不要直接拿来拼路径）；`--filename` 默认取本地文件的 basename，
   上传目录时固定为 `hot-upgrade.tar`（目录要**完整上传**，不能只挑几个文件）。
9. **模块根目录**：`find_images_from_deployments` 读的是模块根目录下的部署文件
   （K8S：`deployments/kubernetes/*deployment.yaml`、`*statefulset.yaml`；PaaS：`docker-compose.yml`）。
   检出目录名不一定等于模块名，**模块名以 `get_environment_info` 的匹配结果为准**，目录名对不上不算失败；
   用「目录里有没有这些部署文件」确认它就是模块根目录。
10. **环境变量变更**：方案里写了就透传 `--env KEY=VALUE`（可重复）与 `--unset-env KEY`（或 `--unset-env ALL`），
    只影响本次热加载流程，不改镜像启动命令；**方案没写就不要设置或取消任何变量**。
11. **一个容器/Pod 只热更新一个模块**：方案涉及多个模块时分别触发，不要合并成一次。
12. **前提是模块正常运行**：Pod 反复重启、Deployment 不存在、容器/Pod 状态异常时，热更新大概率失败，
    先记录并跳过，建议先解决环境问题再重跑。
13. **网络可达性**：容器要能访问 `devops.ks1.wezhuiyi.com`（MCP）与 `pkg.in.wezhuiyi.com`（工具下载与制品上传）。
    连不上属于部署问题，如实记录并跳过，不要在容器里改网络配置。

> 平台原文见内网 Confluence `pageId=195746163`（devops-mcp-invoker 使用说明）；本文只写 `jira-code` 特有的口径。

## 4. 失败与降级

热更新是**推送之后的附加阶段**，任何环节失败都不回滚已经推送的代码，也不把任务判成失败：

| 情况 | 上报与处理 |
|---|---|
| 方案里没有流水线地址 | `--hotreload skipped --hotreload-reason "方案未提供流水线地址"`，结束 |
| 缺少 `DEVOPS_MCP_TOKEN` / MCP 不可用 | `--hotreload skipped --hotreload-reason "未配置流水线凭据"`，结束 |
| `env_type = UAT` / `platform_type = binary` | `--hotreload skipped --hotreload-reason "该环境不支持热更新"`，结束 |
| 找不到可上传的制品 | `--hotreload skipped --hotreload-reason "未找到构建产物（已尝试 <路径>）"`，结束 |
| 上传失败 | 按 devops 技能重试 1 次；仍失败则 `--hotreload failed --hotreload-reason "<错误>"` 并结束，**不写更新记录** |
| 只有唯一父组件候选 | **不跳过**：正常上传、热加载并写更新记录，记录挂在父组件下，`--hotreload succeeded` 的原因里注明是父组件 |
| 匹配到多个父组件候选 / 一个候选都没有 | 列出候选模块，`--hotreload skipped --hotreload-reason "候选不唯一/无候选"`，结束 |
| 部署文件缺失（找不到 `docker-compose.yml` / `deployments/kubernetes`） | `--hotreload skipped --hotreload-reason "检出目录没有部署文件"`，结束；不要凭空编目标环境 |
| 模块不在运行（Pod 反复重启 / Deployment 不存在） | `--hotreload skipped --hotreload-reason "模块不在运行"`，提示先解决环境问题 |
| `$HOME` 不可写导致上传工具下载失败 | 改下到工作区 `work/jira-code/<KEY>/bin/` 并用绝对路径调用；仍失败按 `--hotreload failed` 上报 |
| 连不上 `devops.ks1.wezhuiyi.com` / `pkg.in.wezhuiyi.com` | 属于部署网络问题，`--hotreload skipped` 并记录，不要改容器网络配置 |
| 其他异常 | `--hotreload failed --hotreload-reason "<失败环节与错误信息>"`，结束 |

以上结论统一用 `progress.mjs update --hotreload <succeeded|skipped|failed> --hotreload-reason "<原因>"`
上报，写进**同一条** Jira 评论（标记 `jira-code:progress:<KEY>`），不要为热更新新开评论、也不要在评论正文里
另起一行描述代替上报——评论里那一行「热更新」由脚本按上报结果固定渲染。热更新发生在 §3.7 推送之后、
§3.9 清理检出目录与收尾之前：

- 热更新成功 → `--hotreload succeeded`，原因里带上模块、环境、制品与更新记录，`finish` 的说明里
  **写明怎么关闭热加载**：
  PaaS 在流水线里用当前版本重新更新/部署一次；K8S 要点流水线界面的「重构」，普通重新部署不会移除热加载配置。
  这一步不能省，否则环境会一直挂在热加载状态。
- 热更新跳过或失败 → `--hotreload skipped|failed` 并写明原因（除非任务本身另有异常）。

漏报时 `finish` / `fail` 会把结论兜底成「未执行（未进入热更新阶段）」：这只是防漏报的保险，
真实结果是成功却没上报，评论就会误报成「未执行」。
