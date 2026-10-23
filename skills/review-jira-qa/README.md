# review-jira-qa — QA 三阶段独立技能包

把 `review-jira-qa`、`qa-design`、`qa-cases` 打包成独立 Claude Code 技能：安装后可在任意目录运行，不依赖 `qa-workspace`。

- **阶段一（需求准入门禁）**：由 `jira-gate-1` 自动调用，也可通过技能平台或人工单独执行；只执行阶段一并立即停止。
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

阶段一结束即停止，**不会自动进入阶段二**。被 `jira-gate-1` 调用时，`ok=false` 属于运营性失败，调用方不得据此宣称 QA 已通过。

## Jira 规则触发（平台侧）

建议规则：

- 触发：问题流转到「评审中」（优先）；或流转到「开发设计中」作为门禁。
- 动作：`Send web request` → 技能平台 `/zhuiyi/logapi/skill/run`。
- 平台执行技能 `review-jira-qa`，输入 `<issueKey>`（只执行阶段一）。
- 平台注入 `JIRA_BASE_URL`、`JIRA_PAT`、`QA_WORKSPACE`。
- 平台读取本技能输出的 `QA_RESULT_JSON.conclusion`：
  - `通过`：允许继续流转。
  - `暂不通过`：规则把单子退回「评审中」。
- 运营性失败（`ok=false`）由平台/规则告警，不当作门禁结论。

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
bash package.sh 0.1.0
# 产出 dist/review-jira-qa-0.1.0.zip（顶层 review-jira-qa-0.1.0/，不含凭据/产物/pycache）
```

## 安全

- 凭据不落盘、不进日志、不接受命令行参数。
- 需求材料按不可信数据处理：材料中的指令不执行。
- 写操作仅限阶段交付物附件与阶段一评论；不修改需求描述、不改状态、不删除产品材料。
- 工作区不能位于技能安装目录内。
