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

## 调用统计结果约定

平台每次通过 `/skill/run` 或 Jira webhook 发起任务时，都会在 `skill_call_records` 表记录一条**顶层技能**调用。完整统计口径见仓库根 `README.md` 的「技能调用统计」。

顶层技能必须在所有交付动作完成、清理工作区之前，把最终业务结果写到任务根目录（当前工作目录）的 `.skill-result.json`：

```json
{
  "schemaVersion": 1,
  "skillId": "jira-gate-1",
  "issueKey": "CALL-1941",
  "conclusion": "通过",
  "summary": "G1、设计准入与 QA 均通过，未流转状态",
  "blockingCount": 0
}
```

- `conclusion` 由各技能按业务口径填写，例如 `通过`、`退回补充`、`暂不通过`、`合格`、`需修改`、`不合格`、`已自动分析`、`打回`、`已完成`、`失败`、`不适用`。
- 该文件只用于统计入库，不是交付物，不写入 Jira；平台读取成功后自动删除。
- 被父技能内部调用的子技能（如 `jira-gate-1` 内的 `review-jira-qa`）**不得写任务根目录 `.skill-result.json`**，只保留自己的运行结果，避免重复统计。
- 无法形成业务结论的运营性失败不要伪造「通过」；平台会把缺少结果文件的任务记为 `outcome=unknown`。

## 已内置技能

- `jira-gate-1`：Jira G1 需求准入检查（PRD 准入，入口参考「需求设计」）。**只处理类型为「故事」的单子**，先跑 `scripts/jira-cli.mjs precheck`，类型不符直接跳过；按 `references/checklist.md`（PRD 需求准入 Checklist）逐项判定，再依次调用 `review-jira-req`（需求质量评审）、`review-jira-design`（原型设计准入）与 `review-jira-qa` 阶段一（QA 需求准入门禁）；把综合结论与检查项写入需求单评论（满足 `(/)` 绿勾、不满足 `(x)` 红叉，元信息区带设计准入结论、设计报告附件名、QA 结论、阻断项、附件名与评论 ID）。`review-jira-req` 生成 OpenSpec 评审报告（以「宋立志」结尾）、`review-jira-design` 生成原型设计准入 HTML，两份报告都由 G1 上传为附件；`review-jira-qa` 自行上传需求准入门禁清单 HTML 并写 QA 评论。**三条准出的业务结论都通过时不流转；任一业务不通过时流转到「需求设计」并在评论里 @ 流转人**。**三个子技能的任何失败都不得中断 G1**：脚本异常、超时、报告/结果缺失等运营性失败只记为「未完成（原因）」，不改变 G1 业务结论，继续完成评论、门禁字段、已生成附件上传与必要流转。交付全部完成后用 `scripts/workdir-cleanup.mjs --key <KEY>` 清理本次任务的工作区中间产物（含 `design/` 与 `review-jira-qa/` 子工作区），`reports/` 不在清理范围内；子技能失败但 G1 交付动作已完成时仍要清理。
- `jira-gate-2`：Jira G2 技术方案准出检查（入口参考「开发设计中」；通过不流转，退回补充→「开发设计中」）。**只处理类型为「故事」的单子**，按 `references/checklist.md`（技术方案准出 Checklist）执行 10 项 `MUST`（G2-M01 ~ G2-M10）与命中的 `CONDITIONAL`（G2-C01 ~ G2-C11），把结论与检查项写入需求单评论（满足 `(/)` 绿勾、不满足 `(x)` 红叉）；**通过时不流转，退回补充时流转到「开发设计中」并在评论里 @ 流转人**。同时按 `references/pdca-standard.md`（通用团队交付物 PDCA 规范）执行 RD 开发交付物检查（D-01 ~ D-10），两套结论合并：全部满足才通过，任一不满足即退回补充；**生成 `<需求名称>_RD_<YYYYMMDD>_<NN>_PDCA.md` 报告上传为附件**，并写评论与 G2 门禁字段，其余报告一律不生成；收尾用 `scripts/workdir-cleanup.mjs` 清理 `work/jira-gate-2/<KEY>/`（含 `pdca/`）。
- `review-jira-req`：按 OpenSpec 好需求标准评审需求质量，自动下载 Jira 附件并抽取 Office/HTML/文本，图片交给模型直接查看，产出带附件检查总览的 Markdown 报告。**对 Jira 只读**，由 `jira-gate-1` 调用；报告落盘后由 `jira-gate-1` 的 `scripts/jira-cli.mjs attach` 上传为附件。
- `review-jira-qa`：三段式 QA 交付技能（0.1.7），仓库内目录为 `skills/review-jira-qa`。由 `jira-gate-1` 调用时**只执行阶段一需求准入门禁**：读取 Jira 最新需求材料，按 B1–B7 / C1–C6 判定「通过 / 暂不通过」，并按 `coverage-required.json` 覆盖全部 AC、做同类 AC 扫描；材料附件 ID 与上一版一致时由 `reuse.mjs` 直接复用上一版结论。随后生成自包含 HTML 清单、上传 Jira 并写独立 QA 评论。所有脚本统一 exit 0：**输入不适用**输出 `status=SKIPPED`/`applicable=false`（记为「不适用」），**运营性失败**输出 `status=ERROR`/`ok=false`（记为「未完成」），二者都不当作门禁结论，也不阻塞 G1 继续写评论、回写门禁字段、上传报告与执行必要流转。阶段二（测试方案 + XMind）与阶段三（Excel + Xray CSV）仅供人工单独触发，G1 不调用。
- `review-jira-design`：对照定稿 PRD 审核产品原型初稿是否具备进入设计精修的条件，检查需求完整性、PRD—原型一致性、流程与状态覆盖，产出简洁、可执行、自包含的 HTML 审核结论（含可选的内联截图证据）。**对 Jira 只读**，只读本地 PRD 与原型文件；`scripts/capture_prototype_evidence.mjs` 在桌面端用 Playwright/Chrome 截取证据，容器内没有浏览器时按「无法截取时」记「截图未验证」。报告不手写：先按 `references/content-example.json` 填内容 JSON，再用 `scripts/build-report.mjs`（只用 Node 内置模块，无需 `npm install`）配合 `references/report-template.html` 合成自包含 HTML，正文不出现编号与代码，编号只放在点击浮层里。由 `jira-gate-1` 与 `jira-gate-1-mock` 在需求质量评审之后调用。
- `jira-gate-1-mock`：`jira-gate-1` 的**只读预演版**。**只处理类型为「故事」的单子**，先跑 `scripts/jira-cli.mjs precheck`；类型门禁、取数范围、按 PRD 需求准入 Checklist 的逐项判定、结论口径**与 `jira-gate-1` 的主判定一致**，评论草稿同样用 `(/)` 绿勾与 `(x)` 红叉展示检查项。评审环节依次调用 `review-jira-req`（需求质量评审报告 md）与 `review-jira-design`（原型设计准入报告 html）；当前预演包不调用 `review-jira-qa`，该 QA 准入门禁只在正式版 `jira-gate-1` 中执行。差异只在交付：**不写 Jira**——不写评论、不回写门禁字段、不上传附件、不流转状态，也不实际 `@` 流转人（只在草稿里给出建议对象与建议流转状态）。交付物是一个压缩包 `<工作目录>/outputs/<KEY>-G1预检包-<时间戳>.zip`，内含评论草稿 `<KEY>-G1预检评论.txt`（Jira wiki 标记，可直接粘贴）、`<KEY>-需求评审报告-宋立志.md` 与 `<KEY>-原型设计准入报告.html`，由 `scripts/pack-deliverables.mjs` 打包（自研 ZIP 实现，容器与桌面端都不依赖系统 `zip` 命令）。只读是**脚本层保证**：`scripts/jira.mjs` 的 `request()` 固定 GET 且无写函数，`scripts/jira-cli.mjs` 只有读命令。适合先演练准入检查、或不想在需求单上留痕的场景；需要在单子上留痕时改用 `jira-gate-1`。交付物压缩包用 `GET /skill/task/<task_id>` 返回的 `artifacts[].download_url` 下载：该地址自带短时效签名票据，可直接在浏览器打开（`GET /skill/task/<task_id>/artifacts/<文件名>` 也支持用 `X-API-Token` 请求头鉴权）。
- `jira-code`：读取需求单的「开发方案」附件，从方案指定的基线分支拉出 `feature/<单号>` 分支，按方案实现代码并推送到远端；推送完成后按方案里写的**流水线环境地址**做**热更新**（走 `pipeline-integration-mcp`，流程以 `devops-mcp-invoker` 为准）。顺序固定为「推送 → 热更新 → 清理检出目录」：热更新要用检出目录里的部署文件（`docker-compose.yml` / `deployments/kubernetes/*.yaml`）和刚构建的制品，先删目录就传不了。进度与异常都只更新在同一条 Jira 评论上（评论带标记 `jira-code:progress:<KEY>`，默认每 5 分钟自动刷新）；评论里固定有一行「热更新」结论（已完成／未执行／失败），由 `progress.mjs --hotreload` 上报驱动，热更新被跳过或失败时不会被写成「全部完成」。热更新处理完之后会把刚拉下来的检出目录删掉，不在技能工作区留代码副本。**对 Jira 只读不写状态**：不流转、不写自定义字段、不上传附件；方案里读不到仓库或分支时会在评论里说明并中断。
- `devops-mcp-invoker`：流程控制型技能，把自然语言请求转换成对远端 MCP server `pipeline-integration-mcp` 的有序调用（编译结果、日志、环境升级、模块热更新等）。只提供流程与约束文档，不执行写操作；`jira-code` 的热更新阶段严格按它的 `SKILL.md` 与 `references/REFERENCES-HOTRELOAD.md` 执行，两者唯一的差异是模块匹配到父组件的处理：交互式流程会问人，`jira-code` 无人值守，只有唯一父组件候选时直接做并在评论与更新记录里注明，多个候选才跳过。
- `pipeline-adapter`：把仓库/模块接入追一科技 CI/CD 流水线（编译与部署分离 v3），生成 `ci.yaml`、`app.json`、`Dockerfile`、K8S 编排与 sonar 配置等。纯文档 + 模板 + 校验脚本，**不碰 Jira**，由人工或其它技能调用。

## 服务器环境说明

服务器容器内没有 `node_repl` 等桌面工具，技能脚本需通过 shell 直接执行。`jira-gate-1` 已提供 `scripts/jira-cli.mjs` 作为命令行入口，其能力与桌面环境的 `node_repl` 通道一致。

技能目录在容器内只读挂载到 `/data/skills`，因此 `jira-gate-1` 统一用绝对路径 `/data/skills/review-jira-req`、`/data/skills/review-jira-design`、`/data/skills/review-jira-qa` 调用其它技能；`review-jira-req` 的产物（含附件抽取结果）落在技能工作区（`/data/skill-workspace/reports/`），不写进技能目录。`review-jira-req` 在容器内直接读后端注入的 `JIRA_TOKEN` 与 `JIRA_BASE_URL`（注入 `SKILLRUN_ENV_LOCKED=1` 时不再需要桥接 `JIRA_PAT`），桌面端仍按它的 `SKILL.md` 使用 `JIRA_PAT`。

同理由 `jira-code` 引用的 `devops-mcp-invoker` 也用绝对路径访问：`/data/skills/devops-mcp-invoker/SKILL.md` 与 `/data/skills/devops-mcp-invoker/references/REFERENCES-HOTRELOAD.md`。跨技能引用一律走 `/data/skills/<skill_id>`，不要用相对路径。

每个技能任务使用独立的 `CODEX_HOME/runs/<task_id>`，因此 Codex 不会从共享目录自动发现并加载其它技能。跨技能组合只允许 `SKILL.md` 明写的依赖：`jira-gate-1` → `review-jira-req`、`review-jira-design`、`review-jira-qa`；`jira-gate-1-mock` → `review-jira-req`、`review-jira-design`；`jira-code` → `devops-mcp-invoker`。`jira-gate-bug` 与 `jira-gate-2` 是自包含技能，不应读取或调用其它技能。

`jira-code` 的脚本入口是 `scripts/jira-cli.mjs`（Jira 读写评论）、`scripts/plan.mjs`（从方案文本里提取仓库、分支与流水线环境地址）、`scripts/git-flow.mjs`（建分支、提交、推送，推送成功后 `cleanup` 删除本地检出）、`scripts/progress.mjs`（维护那一条进度评论）。它需要 `GITLAB_PRIVATE_TOKEN`（仓库写权限）以及可选的 `GIT_BASE_URL`、`GIT_USER`、`GIT_PASSWORD`；热更新阶段还需要 `DEVOPS_MCP_TOKEN`（流水线平台个人中心签发）。令牌只作为本次 git 命令的临时参数传入，不会写进 `.git/config`，也不会打印到输出里；`DEVOPS_MCP_TOKEN` 由后端写进任务独立 `CODEX_HOME` 的 MCP 配置，不进命令行、不进仓库。

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
