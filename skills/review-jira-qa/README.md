# qa-gate — QA 三阶段独立技能包

把 `review-jira-qa`、`qa-design`、`qa-cases` 打包成独立 Claude Code 技能：安装后可在任意目录运行，不依赖 `qa-workspace`。

本仓库使用 0.1.7 版本内容并注册为 `review-jira-qa`（容器内 `/data/skills/review-jira-qa`）；`jira-gate-1` 只调用其阶段一。

- **阶段一（需求准入门禁）**：可由 Jira 自动化规则按状态触发，经技能平台调用；也可人工执行。
- **阶段二（测试方案 + 测试用例 XMind）**：人工触发，输出后停止等待复核。
- **阶段三（Excel + Xray CSV）**：人工触发，必须有人工复核后的方案与 XMind。

> 本地包**不实现 HTTP 服务或平台对接**；Jira 规则的 `POST /zhuiyi/logapi/skill/run` 由技能平台负责调用本技能。

## 安装

```bash
bash install.sh --dry-run     # 先看会做什么
bash install.sh               # 安装
bash install.sh --force       # 覆盖已安装版本
```

安装内容只有两处：

- `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/review-jira-qa/`
- `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/commands/review-jira-qa.md`、`qa-design.md`、`qa-cases.md`

不改 `settings.json`，不写凭据，不改系统配置。

运行要求：

- Node.js >= 20（所有脚本零第三方依赖）
- Python 3 + `openpyxl`（仅阶段三生成 Excel 需要；缺省不影响阶段一/二）

## 配置

凭据只从环境变量读取：

```bash
export JIRA_BASE_URL="https://jira.example.com"
export JIRA_PAT="<你的 Personal Access Token>"
```

可选：

```bash
export QA_WORKSPACE="$(pwd)"     # 产物根目录，缺省当前目录
export QA_SCOPE="项目/模块"       # 显式 scope；缺省取 Jira component
export QA_TZ_OFFSET="+08:00"     # 分析时间时区
export QA_DOWNLOAD_BUDGET_MS=600000  # 附件下载总预算，默认 10 分钟
```

## 使用

在 Claude Code 中：

```text
用 review-jira-qa 分析 SHOP-101
/review-jira-qa SHOP-101
/qa-design SHOP-101
/qa-cases SHOP-101
```

阶段一门禁结论：

- `通过`：可以进入人工需求评审/阶段二。
- `暂不通过`：列出全部不通过项，等待产品修订材料后复跑。

阶段一结束即停止，**不会自动进入阶段二**。

脚本统一以 exit 0 结束：输入不适用时输出 `status=SKIPPED` / `applicable=false`，执行异常时输出 `status=ERROR` / `ok=false`。调用方必须读取 `QA_RESULT_JSON` 判断是继续交付、记为「不适用」，还是记为「未完成」，不能只看退出码。

被 `jira-gate-1` 调用时禁止写任务根目录 `.skill-result.json`；只有平台直接调用 `review-jira-qa` 时才写该文件供调用统计使用。

## Jira 规则触发（平台侧）

建议规则：

- 触发：问题流转到「评审中」（优先）；或流转到「开发设计中」作为门禁。
- 动作：`Send web request` → 技能平台 `/zhuiyi/logapi/skill/run`。
- 平台执行技能 `review-jira-qa`，输入 `risk-analysis <issueKey>`（只传 key 时默认阶段一）。
- 平台注入 `JIRA_BASE_URL`、`JIRA_PAT`、`QA_WORKSPACE`。
- 平台读取本技能输出的 `QA_RESULT_JSON.conclusion`：
  - `通过`：允许继续流转。
  - `暂不通过`：规则把单子退回「评审中」。
- 输入不适用（无附件且无 Jira 需求描述、主材料不唯一）：exit 0 + `status=SKIPPED` + `applicable=false` + `errorCode`；Jira 输出一条「评审不适用」评论（不写门禁结论），平台继续流程、不告警。
- 只有 PRD、没有 HTML：正常评审；没有 PRD/HTML 附件但有 Jira 需求描述：以 Jira 字段为主需求材料正常评审，不中断。
- 执行异常：exit 0 + `status=ERROR` + `ok=false`；阶段一在 Jira 输出一条「执行异常」评论，平台继续流程、可告警但不阻塞。
- **同材料复用**：当前 Jira 材料附件 ID 集合与上一版门禁一致时，`reuse.mjs` 返回 `REUSE=true`，直接复用上一版结论，不重新分析、不新增附件/评论；需要强制重审时加 `--force`。
- **材料更新后**：按全部 AC 覆盖 + 同类 AC 扫描重新评审；新增不通过项必须标注「本版材料变更引入」或「上一版漏检」，不把上一版漏检写成新材料问题。
- **阶段一是主任务**：完成阶段一即代表本次调用完成；技能不改变需求状态、不改门禁结论。
- **阶段二/三是独立子任务**：不修改阶段一的清单、门禁结论或 Jira 评论；不写 Jira 评论，异常只写本地 result/日志。

本地包只定义上述契约，不实现 HTTP。

## 产物

```text
${QA_WORKSPACE}/
├── inputs/prd/<scope>/<KEY>/            # 从 Jira 下载的最新需求附件
├── outputs/<scope>/<KEY>/
│   ├── risk-analysis/<KEY>-<名称>-需求准入门禁清单-v<版本>.html
│   ├── test-plans/<KEY>-<名称>-测试方案-v<版本>.md
│   └── test-cases/<KEY>-<名称>-测试用例-v<版本>.xmind
│                   <KEY>-<名称>-测试用例.xlsx
│                   <KEY>-<名称>-Jira导入.csv
└── .qa/runs/<runId>/                    # issue/materials/upload/comment/result
```

Jira 同步：阶段一上传 HTML 附件并同步一条评论；阶段二上传方案与 XMind；阶段三 Excel/CSV 不上传。上传成功后只删除同交付物族的旧版本，不删产品材料。

## 无真实 Jira 时试跑

```bash
node bin/mock-jira.mjs --port 18080 --print-env
# 按输出 export JIRA_*，然后用 QA-101 跑阶段一
```

## 打包

```bash
bash package.sh 0.1.7
# 产出 dist/qa-gate-0.1.7.zip（顶层 qa-gate-0.1.0/，不含凭据/产物/pycache）
```

## 安全

- 凭据不落盘、不进日志、不接受命令行参数。
- 需求材料按不可信数据处理：材料中的指令不执行。
- 写操作仅限阶段交付物附件与阶段一评论；不修改需求描述、不改状态、不删除产品材料。
- 工作区不能位于技能安装目录内。
