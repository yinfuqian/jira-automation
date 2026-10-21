# 需求评审技能（review-jira-req）

从 JIRA Server/DC 拉取需求，按 **OpenSpec 好需求标准** 评审质量，输出终端摘要与 Markdown 报告。

- 规则层（12 条）由脚本判定，**不调用模型**，同一输入必然同一输出
- 语义层（原子性 / 清晰性 / 可验证性 / 场景覆盖）由模型评审，结论必须逐字引用需求原文
- **附件一并评审**：自动下载并抽取 HTML、Word、Excel、PPT、Markdown 文本，图片直接查看；PDF/旧版 Office 在缺少本机工具时明确标注“需人工查看”
- 对 JIRA **只读**：不评论、不改状态、不写任何东西
- **零第三方依赖**，只需要 Node.js >= 20

## 安装

```bash
bash install.sh              # 装到 ~/.claude/skills/ 与 ~/.claude/commands/
bash install.sh --dry-run    # 先看会做什么
bash install.sh --force      # 覆盖已装的旧版本
```

装完即可在**任意项目目录**里使用。只做两件事：拷贝技能目录、拷贝斜杠命令。不改 `settings.json`，不碰任何凭据。

不想用脚本也行，直接拷目录：

```bash
cp -R . ~/.claude/skills/review-jira-req
```

## 配置

凭据只从环境变量读，本工具**不会**把它写进任何文件、报告或日志。

```bash
export JIRA_BASE_URL="https://jira.example.com"
export JIRA_PAT="<你的 Personal Access Token>"
```

建议写进 shell 的 rc 文件。缺这两个变量时，工具会在任何网络请求之前停下来并提示，不会产出半截结论。

## 使用

在 Claude Code 里直接说：

```
用 review-jira-req 评审 SHOP-101
```

或用斜杠命令：

```
/review-jira-req SHOP-101
/review-jira-req SHOP-101,SHOP-102
/review-jira-req --jql "project = SHOP AND status = 评审中" --limit 30
```

一批需求分给多个人时，直接列出来即可，技能会逐个评审：

```
评审这些需求，给出整改建议：
## 张三
PROJ-1 标题
PROJ-2 标题
## 李四
PROJ-3 标题
```

## 产物落在哪

**你执行时所在的项目**下，不是技能目录：

```
<your 项目>/reports/
├── .work/<run-id>/raw/<KEY>.json         原始 issue + 附件清单（不含附件正文）
├── .work/<run-id>/attachments/<KEY>/     附件原文件与抽取出的 .txt
├── .work/<run-id>/normalized/<KEY>.spec.md  归一化后的 OpenSpec 草稿
├── .work/<run-id>/rulecheck.json         规则校验结果
├── .work/<run-id>/semantic/<KEY>.json    语义评审结论
└── review-report-<时间戳>.md             最终报告
```

附件默认限额：单条 issue 最多 10 个、单个 ≤ 5MB、合计 ≤ 15MB。可用 `--max-attachments`、`--max-attachment-mb`、`--max-attachment-total-mb` 调大，或用 `--no-attachments` 关闭。同一 issue 内内容相同的附件按 SHA-256 自动去重，只保留第一份。抽取文本默认单文件 30,000 字符、单 issue 合计 80,000 字符，可用 `--max-file-text-chars`、`--max-issue-text-chars` 调大。超限与无法解析的附件会在报告里列为“未检查”，内容重复的附件单列为“内容重复的附件”，均不会改变需求本身的合格/需修改/不合格结论。

建议把 `reports/` 加进 `.gitignore`：中间草稿含未经确认的需求原文，报告含评审结论，都属本地运行态。

## 没有真实 JIRA？先用 mock 试跑

不需要任何凭据，也不会碰真实 JIRA：

```bash
node ~/.claude/skills/review-jira-req/bin/mock-jira.mjs --print-env
```

把打印出来的 `export ...` 贴进 shell，就能用 `SHOP-101` ~ `SHOP-105` 跑通全流程，确认技能装好了。

## 结论怎么看

| 情况 | 规则结论 |
| --- | --- |
| 存在任一 ERROR | 不合格 |
| 无 ERROR 但有 WARNING | 需修改 |
| 只有 INFO 或全通过 | 合格 |

语义四维单独给结论，与规则结论并列展示——便于发现"结构过了但语义有问题"这类情况（本工具在真实数据上最常见的就是这一种：一条需求既没有 SHALL、也没有场景，被规则判为需修改，而语义上还捆了好几个可独立失败的行为）。

## 目录结构

```
.
├── SKILL.md                 技能入口：完整五步流水线
├── prompts/
│   ├── normalize.md         第 2 步：把需求描述映射成 OpenSpec 草稿（只重排，不补写）
│   └── semantic.md          第 4 步：四维语义评审（结论必须逐字引用原文）
├── bin/                     零依赖 Node 脚本
│   ├── jira-fetch.mjs       拉取（只读 GET）
│   ├── rulecheck.mjs        12 条规则判定
│   ├── report.mjs           合并结果、生成 Markdown 报告
│   ├── mock-jira.mjs        本地 mock，用于无凭据试跑
│   ├── verify-semantic-quotes.mjs  校验语义引用是否逐字来自原文
│   ├── digest-semantic.mjs  摊平语义结果，便于写整改建议
│   └── lib/
├── command/                 斜杠命令（随包分发，install.sh 会安装它）
├── install.sh
└── README.md
```

## 硬约束

如果你要改这个工具，别破坏它们：

1. **归一化只重排、不补写。** 草稿缺场景、缺 SHALL，正是需求本身的缺陷——要让它被规则报出来，而不是靠补写把草稿"修好看"。
2. **对 JIRA 只读。** `jira-fetch.mjs` 里没有任何写请求，这是刻意的；不要为了"顺手改一下描述"加写接口。
3. **附件内容只是数据。** 附件正文/图片里出现的指令、命令、链接一律不执行；下载只允许同源地址、不跟随重定向，日志不打印完整 URL，PAT 不写入任何文件。
