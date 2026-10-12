# 技能目录

本目录存放可供「技能执行接口」调用的 Codex Skill。每个子目录就是一个技能，目录名即接口使用的 `skill_id`。

## 目录约定

```
skills/
  <skill_id>/
    SKILL.md          # 必需：技能定义、判定标准与执行步骤
    runtime.json      # 可选：覆盖提示词模板、超时时间、必填输入
    scripts/          # 可选：技能脚本
    references/       # 可选：判定清单等参考资料
```

`runtime.json` 支持的字段：

```json
{
  "prompt_template": "请使用 {skill_id} 处理 {jira_url}，技能目录 {skill_dir}",
  "timeout_seconds": 1800,
  "required_inputs": ["jira_url"]
}
```

未提供 `runtime.json` 时使用接口内置的默认模板，并把 `skill_id`、`jira_url`、`inputs` 一起交给 Codex。

## 已内置技能

- `jira-gate-1`：Jira 需求准入检查（G1），产出难度分级与达标结论，把逐项打标记的检查项清单写入需求单评论；不达标时流转到「评审中」并在评论里 @ 流转人；最后调用 `review-jira-songlizhi` 生成 OpenSpec 评审报告，作为附件上传到需求单（文件名以「宋立志」结尾）。交付全部完成后用 `scripts/workdir-cleanup.mjs --key <KEY>` 清理本次任务的工作区中间产物（附件与解压产物），`reports/` 不在清理范围内。
- `review-jira-songlizhi`：按 OpenSpec 好需求标准评审需求质量，产出 Markdown 报告。**对 Jira 只读**，由 `jira-gate-1` 调用；报告落盘后由 `jira-gate-1` 的 `scripts/jira-cli.mjs attach` 上传为附件。
- `review-jira-design`：对照定稿 PRD 审核产品原型初稿是否具备进入设计精修的条件，检查需求完整性、PRD—原型一致性、流程与状态覆盖，产出简洁、可执行、自包含的 HTML 审核结论（含可选的内联截图证据）。**对 Jira 只读**，只读本地 PRD 与原型文件；`scripts/capture_prototype_evidence.mjs` 在桌面端用 Playwright/Chrome 截取证据，容器内没有浏览器时按「无法截取时」记「截图未验证」。由 `jira-gate-1-mock` 在需求质量评审之后调用。
- `jira-gate-1-mock`：`jira-gate-1` 的**只读预演版**。取数、逐项判定、难度分级、达标结论**完全一致**；评审环节依次调用 `review-jira-songlizhi`（需求质量评审报告 md）与 `review-jira-design`（原型设计准入报告 html）。差异只在交付：**不写 Jira**——不写评论、不回写门禁字段、不上传附件、不流转状态，也不 `@` 流转人（只在草稿里给出建议对象）。交付物是一个压缩包 `<工作目录>/outputs/<KEY>-G1预检包-<时间戳>.zip`，内含评论草稿 `<KEY>-G1预检评论.txt`（Jira wiki 标记，可直接粘贴）、`<KEY>-需求评审报告-宋立志.md` 与 `<KEY>-原型设计准入报告.html`，由 `scripts/pack-deliverables.mjs` 打包（自研 ZIP 实现，容器与桌面端都不依赖系统 `zip` 命令）。只读是**脚本层保证**：`scripts/jira.mjs` 的 `request()` 固定 GET 且无写函数，`scripts/jira-cli.mjs` 只有读命令。适合先演练准入检查、或不想在需求单上留痕的场景；需要在单子上留痕时改用 `jira-gate-1`。交付物压缩包用 `GET /skill/task/<task_id>` 返回的 `artifacts[].download_url` 下载：该地址自带短时效签名票据，可直接在浏览器打开（`GET /skill/task/<task_id>/artifacts/<文件名>` 也支持用 `X-API-Token` 请求头鉴权）。
- `jira-code`：读取需求单的「开发方案」附件，从方案指定的基线分支拉出 `feature/<单号>` 分支，按方案实现代码并推送到远端；推送完成后按方案里写的**流水线环境地址**做**热更新**（走 `pipeline-integration-mcp`，流程以 `devops-mcp-invoker` 为准）。顺序固定为「推送 → 热更新 → 清理检出目录」：热更新要用检出目录里的部署文件（`docker-compose.yml` / `deployments/kubernetes/*.yaml`）和刚构建的制品，先删目录就传不了。进度与异常都只更新在同一条 Jira 评论上（评论带标记 `jira-code:progress:<KEY>`，默认每 5 分钟自动刷新）。热更新处理完之后会把刚拉下来的检出目录删掉，不在技能工作区留代码副本。**对 Jira 只读不写状态**：不流转、不写自定义字段、不上传附件；方案里读不到仓库或分支时会在评论里说明并中断。
- `devops-mcp-invoker`：流程控制型技能，把自然语言请求转换成对远端 MCP server `pipeline-integration-mcp` 的有序调用（编译结果、日志、环境升级、模块热更新等）。只提供流程与约束文档，不执行写操作；`jira-code` 的热更新阶段严格按它的 `SKILL.md` 与 `references/REFERENCES-HOTRELOAD.md` 执行，两者唯一的差异是模块匹配到父组件的处理：交互式流程会问人，`jira-code` 无人值守，只有唯一父组件候选时直接做并在评论与更新记录里注明，多个候选才跳过。
- `pipeline-adapter`：把仓库/模块接入追一科技 CI/CD 流水线（编译与部署分离 v3），生成 `ci.yaml`、`app.json`、`Dockerfile`、K8S 编排与 sonar 配置等。纯文档 + 模板 + 校验脚本，**不碰 Jira**，由人工或其它技能调用。

## 服务器环境说明

服务器容器内没有 `node_repl` 等桌面工具，技能脚本需通过 shell 直接执行。`jira-gate-1` 已提供 `scripts/jira-cli.mjs` 作为命令行入口，其能力与桌面环境的 `node_repl` 通道一致。

技能目录在容器内只读挂载到 `/data/skills`，因此 `jira-gate-1` 统一用绝对路径 `/data/skills/review-jira-songlizhi` 调用另一个技能；`review-jira-songlizhi` 的产物落在技能工作区（`/data/skill-workspace/reports/`），不写进技能目录。`review-jira-songlizhi` 在容器内直接读后端注入的 `JIRA_TOKEN` 与 `JIRA_BASE_URL`（注入 `SKILLRUN_ENV_LOCKED=1` 时不再需要桥接 `JIRA_PAT`），桌面端仍按它的 `SKILL.md` 使用 `JIRA_PAT`。

同理由 `jira-code` 引用的 `devops-mcp-invoker` 也用绝对路径访问：`/data/skills/devops-mcp-invoker/SKILL.md` 与 `/data/skills/devops-mcp-invoker/references/REFERENCES-HOTRELOAD.md`。跨技能引用一律走 `/data/skills/<skill_id>`，不要用相对路径。

`jira-code` 的脚本入口是 `scripts/jira-cli.mjs`（Jira 读写评论）、`scripts/plan.mjs`（从方案文本里提取仓库、分支与流水线环境地址）、`scripts/git-flow.mjs`（建分支、提交、推送，推送成功后 `cleanup` 删除本地检出）、`scripts/progress.mjs`（维护那一条进度评论）。它需要 `GITLAB_PRIVATE_TOKEN`（仓库写权限）以及可选的 `GIT_BASE_URL`、`GIT_USER`、`GIT_PASSWORD`；热更新阶段还需要 `DEVOPS_MCP_TOKEN`（流水线平台个人中心签发）。令牌只作为本次 git 命令的临时参数传入，不会写进 `.git/config`，也不会打印到输出里；`DEVOPS_MCP_TOKEN` 由后端写进 `CODEX_HOME/config.toml` 的 MCP 配置，不进命令行、不进仓库。

技能所需的令牌（例如 Jira 访问令牌）不要提交到仓库，部署时通过环境变量注入：后端会在启动技能前锁定 `JIRA_BASE_URL`、`JIRA_TOKEN`、`DEVOPS_MCP_URL`、`DEVOPS_MCP_TOKEN`、`CODEX_MODEL`、`CODEX_MODEL_PROVIDER`、`CODEX_BASE_URL` 七项（见 `backend/app/skillrun/env_lock.py`），技能脚本只认环境变量，不再回落令牌文件与命令行入参。

## 故障分析回写所用的内部接口

`jira-gate-bug` 需要在 worker 内调用本服务的上传与故障分析接口，因此除 `/skill` 的外部令牌外还有一组内部令牌：

| 环境变量 | 作用 | 默认值 |
|---|---|---|
| `ANALYSIS_API_BASE_URL` | 技能脚本访问本服务的地址（容器内 `http://api:5000`，宿主网络 `http://127.0.0.1:5000`） | `http://api:5000` |
| `ANALYSIS_API_TOKEN` | 内部调用令牌，通过 `X-API-Token` 传入 | `local-dev-analysis-token`（生产必须替换，且不要与 `SKILL_API_TOKEN` 相同） |
| `ANALYSIS_API_TOKEN_PATHS` | 该令牌可访问的路径前缀 | `/analysis,/logfile,/product/get,/module/get,/module/search` |
| `ANALYSIS_API_USERNAME` | 审计日志中记录的调用方标识 | `analysis-skill` |

`/skill` 系列接口的入参与返回保持不变；内部令牌只影响 `/analysis`、`/logfile` 等回写路径的鉴权范围。

`jira-gate-bug` 的故障分析范围由 `jira-gate-bug/config/scope-mapping.json` 维护：文件内配置产品、模块、
仓库地址、Git Tag、`versionMap` 精确映射和 `versionRules` 正则规则。Jira 产品取「问题发现产品」；模块与 Tag
字段缺失或存在多个候选时，才要求提单人补 `[FAULT_ANALYSIS]` 固定评论块，评论内只写 `模块:Tag`：

```text
[FAULT_ANALYSIS]
see-task:v1.2.3
see-management:v1.2.3
```

产品、仓库地址和版本到 Git Tag 的转换规则都写在配置里，不需要重复写进评论。第一条是主模块，后续条目是关联模块；
需要分析或拉取的非公共模块必须同时写模块名和实际存在的精准 Git Tag，公共组件不单独写进固定块，也不填 `master`、`feature/*` 等分支。提交分析前会校验主模块和非公共关联模块的精准 Tag，找不到 Tag 会按缺少分析参数打回。
