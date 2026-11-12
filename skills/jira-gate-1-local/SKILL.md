---
name: jira-gate-1-local
description: 对 Jira 需求单做 G1 需求准入检查的本地预检版。当用户要求"本地预检 / 预演 / 先不要写入 Jira / 检查 PRD 准入但不要评论或上传附件"时使用。以 jira-gate-1 的 Checklist 和三个子评审流程为准，只读取 Jira 单据与附件；评论、门禁字段、需求评审报告、设计准入报告和 QA 清单只生成本地预检包，禁止写评论、回写字段、上传或删除附件、流转状态。只处理类型为「故事」且产品归属为 Bot / Call / See / Pal / Learn / Aiforce 之一的单子。
metadata:
  short-description: Jira G1 需求准入检查（PRD 准入）
---

# Jira G1 需求准入检查（本地预检）

用户给一个 Jira 需求链接，只读取单据、描述、附件、依赖、子任务和历史评论，按 PRD 需求准入 Checklist 逐项判定，给出**通过 / 退回补充**结论；所有结论只落本地预检包，不向 Jira 做任何写操作。

## 交付物

**交付物只有一个本地预检包**：

1. 本地评论草稿：结论摘要 + 逐项打标记的检查项清单（`(/)` 通过、`(x)` 不通过）；标题必须带「（本地预检）」。元信息区同时写出原型设计准入结论（未上传原型时为「不适用」）、设计报告文件名、QA 准入门禁结论、阻断项数量与 QA 清单文件名；**末尾固定带一行「报告附件」**，指向包内评审报告。
2. 需求评审报告：调用包内 `subskills/review-jira-req` 产出 OpenSpec 评审报告 Markdown，文件名规范为 `<KEY>-需求评审报告-宋立志.md`。
3. 原型设计准入报告（条件交付）：**仅当单子已上传原型材料**时调用包内 `subskills/review-jira-design` 审核 PRD 与产品原型初稿，产出 `<KEY>-原型设计准入报告.html`；未上传原型时不调用该技能、不生成报告，草稿记「不适用（本单未上传原型）」。
4. QA 门禁清单（条件交付）：调用包内 `subskills/review-jira-qa` 阶段一；QA HTML 只进本地预检包，禁止上传 Jira 或写 QA 评论。输入不适用时按「不适用」记录并继续本地交付。
5. 对话里的摘要：单据类型 + 综合结论 + 关键阻断项 + 设计准入结论（无原型时写「不适用」）+ QA 结论 + 本地建议流转 + 预检包绝对路径。

**流转只做预演**（见 §9）：

- 结论为**通过**：G1 Checklist 通过，且 `review-jira-design` 为「通过 / 有条件通过」（未上传原型时为「不适用」，不参与判定）、QA 准入门禁为「通过」；不流转状态，保持当前状态，允许进入技术方案设计。
- 结论为**退回补充**：G1 Checklist、`review-jira-design`（有原型且已执行时）或 QA 准入门禁任一不通过；只在草稿与摘要中建议流转到「需求设计」，并记录建议处理人文本，不真实 `@`、不流转。

评论以**可执行、可扫读**为目标：让人 30 秒内顺着红叉看清「卡在哪、要补什么」，绿勾项一眼带过。

**所有报告必须留在本地预检包，禁止上传 Jira。** §11 的评审报告 md、§11.1 的原型设计准入报告 HTML（仅单子已上传原型时生成）与 §11.2 的 QA 清单 HTML 都是预检包内容；评论草稿也只写本地文件。

中间附件和解析文件放在 `<工作目录>/work/jira-gate/<KEY>/`；预检包放在 `<工作目录>/outputs/`。交付完成后只清理中间产物，保留 outputs 预检包；子技能失败不等于预检失败，仍要正常收口。

## 判定依据

检查项、等级（`MUST` / `CONDITIONAL` / `SHOULD` / `OBSERVE`）与判定原则以 [references/checklist.md](references/checklist.md)（PRD 需求准入 Checklist）为准。**先读它再开始判定**，不要凭印象发明标准。

核心口径：

- **状态流转参考：通过时预演为不改变状态；退回补充时建议流转到「需求设计」（当前已在需求设计时建议不重复流转）。本技能不调用流转接口。**
- 每次都执行全部 **8 项 `MUST`** 与所有**命中的 `CONDITIONAL`**，不抽样、不使用总分或平均分。
- 单项结果只允许「满足 / 不满足 / N/A」；证据不足、语义冲突或无法唯一判断按**不满足**处理。
- `N/A` 只适用于未命中的场景项，并必须记录不适用依据。
- 任意适用 `MUST` 或命中 `CONDITIONAL` 不满足，结论只能是**退回补充**。
- `SHOULD` 不单独阻断，但需在清单里记录取舍；`OBSERVE` 只记录进入研发后仍需关注的假设或风险，不能代替缺失的业务事实。
- 判定要过「**同一结果测试**」：同一业务条件下，两名合格研发仅依据 PRD 会得出不同的用户结果、权限、状态、数据含义、失败行为或 AC，即业务逻辑未闭环，必须退回补充。
- 只判断产品业务逻辑能否唯一指导研发设计和测试验收；**不检查文档格式**，PRD 可以用段落、列表或表格，不强制状态权限矩阵、追溯矩阵、原型或图示。
- **判定由准出共同组成**：本 Checklist 是 G1 主判定；`review-jira-design` 审核 PRD 与原型初稿的一致性（**仅当单子已上传原型材料时执行**，未上传时记「不适用」且不参与综合结论）；`review-jira-qa` 的阶段一按 B1–B7 / C1–C6 输出 QA 结论。执行了的准出都进本地预检包，任一**业务结论**为「不通过 / 退回补齐 / 审核未完成 / 暂不通过」都使 G1 最终结论为**退回补充**；`review-jira-design` 的「有条件通过」不阻断，子技能的运营性失败只记为「未完成」且不阻断。

## 执行步骤

### 0. 运行环境适配（先读这一节）

本技能是本地预检版，只允许 Jira REST 读取通道；禁止使用应用内浏览器提交评论，也禁止启用平台写路径和子技能自带的 mock/selftest 安装入口。判定标准与 `jira-gate-1` 完全相同，两种运行环境的取数通道如下：

- **桌面 Codex（带 `node_repl` 工具）**：按下面「通道 A」的 `node_repl` 片段执行。
- **服务器容器 / `codex exec`（没有 `node_repl`）**：用 shell 直接执行技能自带命令行入口 `scripts/jira-cli.mjs`（能力与 `node_repl` 通道一致）：

  ```bash
  # 令牌与连通性自检；令牌来自 --token、JIRA_TOKEN 环境变量或技能目录下的 .jira-token
  node <技能目录>/scripts/jira-cli.mjs selftest
  # 强制预检：类型为「故事」且产品归属在 Bot/Call/See/Pal/Learn/Aiforce 内才继续（见 §1）
  node <技能目录>/scripts/jira-cli.mjs precheck <KEY|URL>
  # 读取需求单并输出紧凑 Markdown 摘要（判定依据）
  node <技能目录>/scripts/jira-cli.mjs get <KEY|URL>
  # 下载附件到工作目录
  node <技能目录>/scripts/jira-cli.mjs attachments <KEY|URL> work/jira-gate/<KEY>/attachments
  # 判定单子是否上传了原型材料，决定要不要执行 review-jira-design（见 §3.1）
  node <技能目录>/scripts/detect-prototype.mjs --dir work/jira-gate/<KEY>/attachments --json

  # --- 1) review-jira-req 五步流水线生成报告 md，再取评审结论（见 §11）---
  # …（fetch / normalize / rulecheck / semantic / report.mjs 详见 §11）
  node <技能目录>/scripts/report-conclusion.mjs <报告路径> <KEY>   # → 不合格 / 需修改 / 合格

  # --- 2) 仅 hasPrototype=true 时：review-jira-design 审核 PRD—原型，生成 HTML 报告（见 §11.1）---
  # …（读 SKILL.md → 建编号索引 → 写内容 JSON → build-report.mjs）

  # --- 3) review-jira-qa 阶段一，读取 QA_RESULT_JSON 得到 QA 结论（见 §11.2）---
  # …（preflight / jira-read / jira-download / materials / render / validate / upload / comment / result）

  # --- 交付顺序：三个评审技能 → 本地评论草稿 → 打包本地预检包（见 §12）---
  # 不执行 comment / gate-set / attach / attach-prune / transition 等任何写命令。
  ```

  附件解析仍走 Python：`python <技能目录>/scripts/extract.py <附件目录> --out <附件目录>/../attachments.txt`。

  自检中「校验令牌」步骤返回 401 只表示该令牌无权访问 `/myself` 账户接口，**不等于令牌失效**：只要「访问 Jira」步骤成功、且 `get <KEY|URL>` 能取到需求单，就继续按流程完成检查。

  在该环境中不要尝试调用 `node_repl`，也不要因为缺少桌面工具而降低判定标准。

### 1. 单据类型与产品归属预检（强制，先做）

**只处理同时满足以下两个条件的 Jira 单子：**

1. 类型（`issuetype`）为「故事」；
2. 产品归属（`产品归属`，旧单兼容 `问题发现产品` / `发现产品`）属于 `Bot`、`Call`、`See`、`Pal`、`Learn`、`Aiforce` 之一（大小写不敏感，多值字段只要命中一个即可）。

在读取附件、调用任何其他技能、写评论、回写字段或流转之前，先执行：

```bash
node <技能目录>/scripts/jira-cli.mjs precheck <KEY|URL>
```

返回示例：

```json
{
  "ok": true,
  "key": "CALL-1446",
  "issueType": "故事",
  "expected": "故事",
  "typeSupported": true,
  "product": "Call",
  "productField": "产品归属",
  "products": ["Call"],
  "expectedProducts": ["Bot", "Call", "See", "Pal", "Learn", "Aiforce"],
  "productSupported": true,
  "supported": true,
  "status": "需求设计",
  "summary": "..."
}
```

- `supported: true`（`typeSupported=true` 且 `productSupported=true`）→ 继续 §2。
- `supported: false` 或任一字段为空 → **立即停止**：不写评论、不回写门禁字段、不上传附件、不流转状态、不调用 `review-jira-req`、`review-jira-design` 或 `review-jira-qa`，也不清理（本次没有产生中间产物）。只在对话里说明「该单类型为 X、产品归属为 Y，不在『故事 + Bot/Call/See/Pal/Learn/Aiforce』范围内，按规则跳过，未对 Jira 做任何写操作」。
- 不要用概要、标签、模块或描述去猜测类型与产品归属；只认 Jira 的 `issuetype` 与产品归属字段。字段缺失一律视为不满足。
- `productField` 用于说明值取自哪个候选字段（`产品归属` / `问题发现产品` / `发现产品`），便于排查取数异常。

### 2. 解析单号

`parseIssueKey()` 支持 `/browse/KEY`、`?selectedIssue=KEY`、jql 链接、纯 `KEY` 四种输入。

### 3. 取数：通道 A —— Jira REST（首选）

在 `node_repl` 工具里执行下面的代码。**注意本机沙箱限制**：PowerShell 无法建立 TLS 连接，`node_repl` 是唯一能访问 Jira 的通道；`node_repl` 内没有 `process`、不支持静态 import，所以脚本必须用 `await import()` 动态加载，且**不要在 node_repl 顶层引用 `process`**。

```javascript
// <SKILL_DIR> = 本 SKILL.md 所在目录的绝对路径（安装后通常是 ~/.codex/skills/jira-gate-1-local）
const j = await import("file:///<SKILL_DIR>/scripts/jira.mjs");
const st = await j.selftest();              // 先自检：令牌、连通性、账号
nodeRepl.write(JSON.stringify(st, null, 1));
```

自检通过后取数：

```javascript
const check = await j.precheckIssueType("CALL-1446");   // 类型不是「故事」或产品归属不在清单内时必须立即停止
const issue = await j.getIssue("CALL-1446");            // 也接受完整 URL
nodeRepl.write(j.renderIssueMarkdown(issue));           // 紧凑摘要，不要直接打印原始 JSON
const files = await j.downloadAttachments(issue.key, "<工作目录>\\work\\jira-gate\\CALL-1446\\attachments", { issue });
```

- `getIssue()` 返回规范化对象：描述、自定义字段（中文名）、附件、问题链接、子任务、评论、工时。
- `renderIssueMarkdown()` 是控制上下文的关键，用摘要做判定；只有摘要被截断时才回到原始对象取局部字段。
- 附件下载后用内置 Python 解析文本（脚本只支持动态导入，Python 走 PowerShell）：

```powershell
# $py 用 load_workspace_dependencies 返回的 Python 路径；<SKILL_DIR> 同上一层说明
& $py "<SKILL_DIR>\scripts\extract.py" "<附件目录>" --out "<附件目录>\..\attachments.txt"
```

若环境里已有可用的 `python`（需带 pdfplumber / python-docx / openpyxl / python-pptx），直接 `python extract.py ...` 也可以。

支持 `docx / pdf / xlsx / pptx / txt / md / json / xml / csv / html`；**解析失败要如实记为"无法解析"**，不能当成"附件没有内容"。图片附件用图像查看能力看，不做臆测。

附件是压缩包时脚本会先解压再递归解析（`.zip / .rar / .7z / .tar / .tar.gz / .tgz / .gz / .bz2 / .xz`），解压产物放在 `<附件目录>\__unpacked\`，输出里用 `压缩包名/内部路径` 标明每个文件的来源：

- **`.rar` 是重点**：服务端镜像已预装 `bsdtar`（libarchive-tools）与 7-Zip 系命令，脚本按 `bsdtar` → `7z` → `7zz` → `7za` → `unar` → `unrar` → `tar` 顺序降级，**容器内无需也无法通过联网安装来解决解压问题**；Windows 桌面端则由系统自带的 `tar.exe`（同为 bsdtar）兜底。
- **不要安装软件**：解压工具、Python 与 Node 依赖都已在镜像内备好。遇到工具缺失时如实记录并说明，不要执行 `apt-get` / `pip` / `npm` 等安装命令。
- 引用证据要写到压缩包内部，例如 `附件.rar/xxx_PRD_DELIVERY.md §5.2`、`附件.rar/xxx_Prototype.html`。
- 解压失败时会输出 `[压缩包解压失败]` 及尝试过的命令与报错，**这种情况必须如实记为"附件无法解析"，不能当成"附件没有内容"，也不要臆测包内内容**。
- 可选 `--unpack-dir <目录>` 指定解压位置；默认的 `__unpacked` 目录在遍历时会跳过，不会重复解析；同名解压结果自动加 `-2` 后缀，不覆盖上一次结果。

### 3.1 原型材料判定（决定是否调用 review-jira-design）

**只有单子里确实上传了原型材料，才调用 `review-jira-design`。** 附件下载并解压完成后，执行：

```bash
node <技能目录>/scripts/detect-prototype.mjs --dir work/jira-gate/<KEY>/attachments --json
```

输出 JSON 的关键字段是 `hasPrototype`、`reason`、`hits`、`ambiguous`、`ignored`。判定口径：

- **先排除自生成的历史产物**：只按本流程专属标记识别——`*原型设计准入*.html`（含旧命名「原型设计准入审核」）、`*需求评审报告*`、`*QA需求准入门禁清单*`、`*G1预检评论*`/`*G1准入结论*` 等，即使文件名或正文含「原型」也统一进 `ignored`，不参与 `hasPrototype`。**「测试用例」「测试方案」「评审报告」「PDCA」等只是业务主题词，不能作为排除依据**：真实原型可能就叫「测试用例管理」「PDCA 看板」，把它们判成历史产物会漏掉设计审核；这类 HTML 会进 `ambiguous` 交人工确认。
- 文件名或相对路径含 `原型`、`prototype`、`mockup`、`wireframe`、`axure`、`figma`、`蓝湖`、`墨刀`、`交互` 等关键词 → 视为已上传原型。
- 图片附件（`png/jpg/jpeg/gif/webp/bmp/svg`）且文件名不像 PRD、评审报告、技术方案等文档 → 视为已上传原型图。
- `html/htm` 附件：路径或页面 `<title>`/`<h1>` 命中关键词 → 视为已上传原型初稿；只有正文提到「原型」、标题却是需求/PRD 等文档的，不命中。
- 未展开的压缩包、无关键词的 HTML、正文提到原型但标题无法判断的 HTML（`html-keyword`）→ 进入 `ambiguous`，需要先解压，或打开该 HTML 人工确认是不是原型；确认是业务页面才执行 §11.1，确认是 PRD/说明文档则忽略。

处理规则：

- `hasPrototype=true` → 按 §11.1 正常执行设计审核。
- `hasPrototype=false` 且 `ambiguous` 为空 → **跳过 §11.1**：不调用 `review-jira-design`、不生成也不上传设计报告，评论记「原型设计准入：不适用（本单未上传原型）」「设计报告附件：不适用（本单未上传原型）」，**同时按 §11.1 清理历史残留的设计报告，保证附件区只剩当前有效产出（该族此时应为 0 份）**，继续 §11.2 与后续交付。
- `hasPrototype=false` 但 `ambiguous` 非空 → 必须逐个确认（解压压缩包、打开 HTML 看是不是原型页面）；确认后仍没有原型才可跳过，并在评论里写明判定依据（例如「仅有 PRD.html 与未含原型的压缩包」）。**`ambiguous` 里如果是已知的 QA 清单、评审报告、技术方案等非原型材料，可直接判定为非原型，不必逐个打开。**
- **不得把「找不到业务原型」写成「审核未完成」**：`审核未完成` 只用于「确有原型、但 PRD 缺失/无法读取/对象无法唯一定位」。如果核对后发现命中的只是历史报告或非业务页面，必须回到 `hasPrototype=false` 的「不适用」分支，不得生成设计报告，也不得把缺少原型计为 G1 阻断项。
- 判定结论要写进任务记录与对话摘要，便于追溯为什么没执行设计审核。

### 4. 取数：通道 B —— 应用内浏览器（无令牌时兜底）

没有配置令牌时，用已登录的应用内浏览器打开需求单：

1. `cua.createBrowserTab("iab", issueUrl)` 打开单子。
2. 用 `tab.playwright.evaluate(() => document.body.innerText)` 取正文，可拿到概要与描述、附件列表、问题链接、子任务、评论；**同时确认页面上的「类型」字段是「故事」、「产品归属」属于 Bot/Call/See/Pal/Learn/Aiforce 之一**，任一项不满足就立即停止。
3. 附件：点击附件链接触发下载，再对下载目录跑 `extract.py`（压缩包会自动解压，见上一节）；随后用 `detect-prototype.mjs --dir <附件目录>` 判定是否上传了原型，决定要不要执行 §11.1。
4. 结论评论：在页面评论区输入并提交。

注意：Jira 的 `/rest/...` 地址在浏览器里会被拦截（`ERR_BLOCKED_BY_CLIENT`），**不要在浏览器里访问 REST 地址**；浏览器只用于读页面和写评论。通道 B 的结论必须注明"基于页面内容检查"。

### 5. 逐项判定

对 `references/checklist.md` 的每条检查项给出：`满足` / `不满足` / `N/A`，并写明**证据位置**（PRD 章节、字段名、描述段落、附件名，必要时页码或行号）。

**必须覆盖的范围**：

- §5 全部 **8 项 `MUST`**：事实源、目标与范围、核心链路、业务规则、状态与权限、业务数据、异常恢复、验收标准。
- §6 全部 **`CONDITIONAL`**：UI 与字段、角色/状态与高风险确认、跨系统/异步/外部依赖、迁移/历史兼容、LLM 业务质量与失败策略、性能/安全/隐私与合规。命中的逐条判定；未命中的写 `N/A` 并给出未触发依据。
- §7.1 `SHOULD`：逐条记录取舍，不阻断结论。
- §7.2 `OBSERVE`：只记录不会改变当前业务规则和验收结论、但研发或发布阶段需要继续验证的事项。

判定要求：

- 结论必须能指向具体证据；找不到证据就是"不满足"，不许写"已满足"。
- 答案若已存在于需求单、附件或历史评论中，自己核对出结论，不要把检索工作反问给产品经理。
- 现有资料无唯一答案、且不同答案会改变产品规则/行为/状态/权限/AC 的，列入"业务待决策事项"（最多 5 项），并给出具体问题与推荐方案。
- 纯技术实现细节（Java 类、接口实现、数据库字段、SQL、组件、线程池、部署参数、模型 Schema、实验配置等）不作为 G1 阻断理由；缺这类内容只记为技术专项输入，交研发。
- 原型、图示、文档排版、追溯矩阵等缺失，但 PRD 业务语义可唯一理解的，不作为本 Checklist 的独立阻断项；**单子未上传原型材料时按 §3.1 跳过设计审核并记「不适用」，不阻断 G1**；已上传但不可读、无法核对时，才由 `review-jira-design` 记为「审核未完成」并参与综合结论。
- 不允许把 Jira 单据字段缺失、测试数据未准备包装成 G1 阻断项。

### 6. 得出准入结论

- **通过**：8 项 `MUST` 与所有命中的 `CONDITIONAL` 均满足，且不存在会改变用户结果、规则、状态、权限、数据含义、失败行为或 AC 的未决业务问题。
- **退回补充**：任一适用 `MUST` 或命中 `CONDITIONAL` 不满足；或存在未闭合的核心业务歧义；或关键及 P0/P1 FR、用户可见的 LLM 结果缺少可客观判断的 AC。
- `SHOULD` 缺失只记录取舍，不单独阻断；`OBSERVE` 不得替代缺失的业务事实。
- 结论只允许两个取值：`通过` / `退回补充`。不要用总分、平均分或"基本通过"等模糊结论。
- 该结论先按本 Checklist 得出；§11.1 的 `review-jira-design`（仅单子已上传原型时执行）、§11.2 的 `review-jira-qa` 跑完后再合并：设计的**业务结论**为「退回补齐 / 审核未完成」或 QA 的**业务结论**为「暂不通过」时，最终 G1 结论强制改为**退回补充**；设计为「通过 / 有条件通过」或「不适用（未上传原型）」且 QA 为「通过」时不改变 G1 自身结论。
- 三个子技能的**运营性失败**都不改变 G1 自身结论：`review-jira-req` 任一步失败记为「未完成（阶段/原因）」，`review-jira-design` 仅在单子已上传原型时执行，其脚本非零退出或内容 JSON 缺失记为「未完成（原因）」，未上传原型记「不适用」，`review-jira-qa` 的 `status=ERROR` / `ok=false` / 结果缺失记为「未完成（status/errorCode/error）」，QA 输入不适用记为「不适用」。这些结果都只作为未完成说明写入 G1 评论，不得写成通过，也不得包装成业务阻断项。

### 6.1 子技能失败统一收口（硬约束）

**本技能调用的所有子技能都不得中断 G1 本地预检流程。** 无论是非零退出、超时、脚本异常、结果文件缺失、结果 JSON 非法，还是未产出报告，都按以下方式收口，然后继续执行后续子技能与本地打包动作：

- **`review-jira-req`**：五步流水线任一步失败、报告未生成或 `report-conclusion.mjs` 提取失败时，需求评审结论记为「未完成（阶段/原因）」，报告附件写「未生成（原因）」；继续执行 `review-jira-design`、`review-jira-qa`、G1 评论、门禁字段、可上传附件与必要流转。
- **`review-jira-design`**：只有「确有原型」但因 PRD 缺失/不可读、原型材料不可读、对象无法唯一定位或证据不足导致的 `审核未完成` 属于**业务结论**，仍会使最终结论为「退回补充」；**原型本身未上传时走 §3.1 的「不适用」分支，不属于审核未完成**。脚本非零退出、`content.json` 缺失/非法、报告未生成属于**运营性失败**，记为「未完成（原因）」，不改变 G1 自身结论，也不阻止后续交付。
- **`review-jira-qa`**：`status=SKIPPED` 记为「不适用」，`status=ERROR` / `ok=false` / `result.json` 与 `QA_RESULT_JSON` 均缺失记为「未完成（原因）」，都不属于业务阻断，也不阻止后续交付；`conclusion=暂不通过` 属于业务结论，仍使最终结论为「退回补充」。
- 子技能失败**不等于** G1 交付失败：不得因此停止后续子技能、停止写评论、跳过门禁字段或跳过附件上传；已完成的 Jira 写操作不回滚，不等待人工确认。
- 只有 G1 自身的评论、门禁字段、附件上传或状态流转动作在重试后仍无法完成时，才按对应章节记录实际结果并保留现场；这也不改变已经得出的 G1 业务结论。

### 7. 组织评论内容

按本文档「输出模板」节的模板组织正文。**通过与退回补充用同一套结构**，差别只在标题措辞、每项标记与阻断项说明。评论正文包含：

- 顶部结论摘要：结论、结论说明、PRD 路径与版本、原型设计准入结论、设计报告文件名、业务待决策事项、架构师澄清、研发技术澄清、复审范围、确认人/时间、预检时间、建议流转。
- QA 门禁摘要：`review-jira-qa` 结论、阻断项数量、HTML 文件名；本地预检不写 QA 评论 ID，固定记录「未写入（本地预检）」。
- 检查项判定明细：按 `MUST` / `CONDITIONAL` / `SHOULD` 分组的逐项清单，每项一行、行首必须带标记。
- 末尾固定一行「报告附件 + 评审结论」。

**检查项清单的写法**

- 标记：满足写 `(/)`（绿勾），不满足写 `(x)`（红叉）。两者都必须显式写出，不允许留空或省略。
- 满足项：只写 `(/) *编号 名称*（等级）`；确有必要说明边界时，最多补一句 30 字以内的结论。
- 不满足项：写 `(x) *编号 名称*（等级）：问题 → 要补什么；证据：位置`。
- 未命中的条件项：写 `(/) *编号 名称*（CONDITIONAL，N/A）：判断依据`。N/A 不构成阻断，所以用绿勾并在括号里标明 `N/A`。
- `SHOULD` 项不阻断：满足用 `(/)`，缺失用 `(x)` 并在括号里注明「SHOULD，不阻断 G1」。
- 排列顺序按 checklist 章节号（5.1 → 5.8 → 6.1.1 → … → 7.1.1），便于与清单逐条对照。
- 判定过的项都要出现，**不得只列不通过项**；不通过项不做删减，精简的是表述，不是缺口。
- 不要用表格（列宽大、手机上难读），用有序列表。
- 评论总长建议 ≤ 3000 字符；检查项多时可放宽到 4500。

**不写**（这些留在对话回复里）：判定过程说明、`OBSERVE` 的完整观察计划、报告正文内容。

### 8. 写本地评论草稿

- 将 `work/jira-gate/<KEY>/comment.txt` 写成本地草稿；标题必须带「（本地预检）」，不提交到 Jira。
- 复审同一张单子时，可用只读 `reviews <KEY|URL>` 对照历史结论；草稿标题改为「G1 需求准入结论（本地预检）（复审）」，不修改或回复任何历史评论。
- 通过与否都写入同一套检查项清单：通过时标题写「通过（允许进入开发设计中）」，各项按实际判定打 `(/)` 或 `(x)`。
- 结论为**退回补充**时，草稿按 §9 在元信息区带「建议处理人」文本；`[~登录名]` 只作为文本，不真实 @。
- 草稿**末尾固定带一行「报告附件」**（通过与否都要有）：文件名按 §11 规则确定，`评审结论` 用 `report-conclusion.mjs` 从报告 md 提取（不合格 / 需修改 / 合格），**不许自己猜**。报告未生成或结论提取失败时，`评审结论` 固定写「未完成（阶段/原因）」，报告写「未生成（原因）」，并继续生成草稿。
- 设计准入摘要（结论、报告文件名）只能来自 §11.1 的内容 JSON 与实际生成的本地文件，不许自己猜；设计审核未完成时不得写「通过」。
- QA 摘要三行（结论、阻断项、清单文件名）只能来自 §11.2 的 `result.json` / 脚本输出，不许自己猜；QA 未完成时不得写「通过」。
- 完整顺序见 §9 第 4 条：**review-jira-req → review-jira-design（仅单子已上传原型时）→ review-jira-qa → 写本地评论草稿 → 打包全部交付物**。

### 9. 交付顺序与「仅退回补充时建议流转」

**第 4 条的交付顺序适用于全部结论；第 1～3、5 条的流转建议与处理人文本只在结论为「退回补充」时写入草稿。** 结论为「通过」时：不写建议处理人，也不建议流转，保持当前状态；本地评论草稿、报告和预检包照常生成。

1. 取建议处理人：`resolveFlowOwner()`，命令行即 `flow-owner <KEY|URL>`。**口径就是经办人**：优先取 `assignee`；经办人为空时才依次回退 `reporter` → `creator`，回退取到时必须在对话摘要里写明实际字段来源，不要静默建议给别人。返回的 `mention` 形如 `[~liu.huan]`，只作为文本写进草稿。
2. 写本地草稿：退回补充模板在元信息区固定多一行「建议处理人」（含 `mention` 文本），见「输出模板」节的示例。
3. 预演流转：
   - 结论为**通过**：不建议流转，不调用 `transitionToStatus()`；草稿里写「预演未流转（通过，保持当前状态）」。
   - 结论为**退回补充**：目标状态名固定建议为「需求设计」，不要按项目改写成别的状态，也不要用「评审中」「待办」「开发中」等替代；可用只读 `transitions <KEY|URL>` 核对当前是否存在该流转。
4. **顺序固定为：`review-jira-req` 五步流水线 → `review-jira-design` 原型设计准入（仅单子已上传原型时）→ `review-jira-qa` 阶段一 → 写本地评论草稿 → 打包全部交付物**。三个评审技能都必须在草稿完成前跑完（设计一步在无原型时记「不适用」），因为草稿要引用需求评审结论、设计准入结论与 QA 结论；QA 的 HTML 和建议评论只保留本地，不上传、不提交。
5. 草稿里「状态流转：」固定写「预演未执行」。结论为退回补充时另写「建议流转：需求设计（预演未执行）」；建议处理人只写文本，不真实 @，也不因预检失败删除该建议。

### 10. 门禁字段只做预演

若 Jira 侧已配置 G1 门禁字段，只允许用只读接口查看当前值；**禁止调用 `gate-set` 或 `setGateResult()`**：

```javascript
const gate = await j.getGateResult(key);       // 只读当前门禁字段，用于预演对照
nodeRepl.write(JSON.stringify(gate));
```

- 字段不存在时返回 `{ skipped: true, reason, hint }`：照常生成本地草稿，并在摘要里说明「当前未配置门禁字段」。
- 字段名默认按 `G1准入结论` / `G1预检时间` 依次匹配；实际字段名不同时，在 `~/.codex/jira/config.json` 用 `gateResultField` / `gateCheckedAtField` 覆盖，不要改脚本。
- 取值约定：通过写 `通过`，退回补充写 `不通过`。
- 本版本不再产出需求难度，**不要写 `G1需求难度`**；该字段若存在也保持原值。
- 草稿里固定写「门禁字段：预演未写入」，可在摘要中给出当前值与本次建议值。

### 11. 评审报告（review-jira-req）与附件上传

G1 评论只给门禁结论；需求质量的细读结论由同仓库的 `review-jira-req` 技能产出。**它的产物是 Markdown 报告，必须作为附件上传到需求单**——只留在本地不算完成。

**技能位置**：本整合包内固定为 `<技能目录>/subskills/review-jira-req`。读取该子技能的判定标准、提示词与五步流水线时，**把它文档中的默认技能目录替换为本包路径**，不要访问 `/data/skills/review-jira-req`。**它自身对 Jira 只读**（`bin/` 里没有任何写请求），报告由本技能收进本地预检包，不要改动它的脚本。

**附件分工**：`review-jira-req` 的 fetch 步骤会自行下载附件到 `reports/.work/<run-id>/attachments/<KEY>/`，并抽取文本或保留图片供 normalize/semantic 使用；本技能此前为 G1 判定下载到 `work/jira-gate/<KEY>/attachments/` 的副本仍保留。两边目录用途不同，不要用 `--no-attachments` 关闭它的附件读取，否则报告会缺少附件证据和检查总览。

**环境桥接**（桌面端它读 `JIRA_PAT`；容器内后端已注入 `SKILLRUN_ENV_LOCKED=1`，它直接读 `JIRA_TOKEN`，下面几条命令带上也无副作用）：

```bash
export SKILL_DIR="<技能目录>/subskills/review-jira-req"
export JIRA_PAT="$JIRA_TOKEN"
export JIRA_BASE_URL="${JIRA_BASE_URL:-https://jira.in.wezhuiyi.com}"
```

**五步流水线**（产物落在技能工作区的 `reports/` 下，不写进技能目录）：

```bash
node "$SKILL_DIR/bin/jira-fetch.mjs" --key <KEY>              # 1 拉取，记下输出的 run-id
# 2 归一化：按 prompts/normalize.md 写 reports/.work/<run-id>/normalized/<KEY>.spec.md（只重排、不补写）
node "$SKILL_DIR/bin/rulecheck.mjs" --only-rules --run-id <run-id>   # 3 规则校验
# 4 语义评审：按 prompts/semantic.md 逐条写 reports/.work/<run-id>/semantic/<KEY>.json
node "$SKILL_DIR/bin/verify-semantic-quotes.mjs" <run-id> <KEY>      # 引用逐字自检（硬门禁）
node "$SKILL_DIR/bin/report.mjs" --run-id <run-id>          # 5 生成 reports/review-report-<时间戳>.md
```

**这一步要在写本地草稿之前做完**：草稿末尾的「评审结论」必须从这份 md 里提取，不许先猜。提取用本技能自带的确定性脚本，只输出三个取值之一：

```bash
node <技能目录>/scripts/report-conclusion.mjs "reports/review-report-<时间戳>.md" <KEY>
# → 不合格 / 需修改 / 合格（取值非法或找不到该 KEY 时以退出码 1 报错，如实说明，不要猜一个值）
```

**失败收口**：五步流水线任一步非零退出、报告未生成、结论提取失败或超时，都属于运营性失败。把需求评审记为「未完成（阶段/原因）」，不把该报告放入预检包，不中断 G1；按 §3.1 的原型判定继续（有原型才执行 `review-jira-design`）、继续 `review-jira-qa` 以及后续本地草稿和打包。不得编造 `评审结论`，也不得把脚本故障写成 G1 业务阻断项。

**报告命名为硬性要求**：`<KEY>-需求评审报告-宋立志.md`，**必须以「宋立志」结尾**，扩展名保持 `.md`。脚本产出的文件名带时间戳，打包时由脚本复制成规范名。

- **不调用 `attach` / `attach-prune` / `attach-list`**；报告只进入本地预检包，打包后通过包内条目清单读回确认。
- 顺序固定为：**review-jira-req → review-jira-design（仅单子已上传原型时）→ review-jira-qa → 写本地评论草稿 → 打包全部报告**。三个评审技能对所有结论都执行（未上传原型时设计这一步记「不适用」）；任何一步失败都不回滚已完成的部分。全部完成后按 §12 清理工作区中间产物。
- 报告名里不出现空格、斜杠等特殊字符；不要用「宋立志」以外的后缀，也不要省略它。
- 报告 md 是本地交付物：不贴进评论草稿；原时间戳文件可留在 `reports/` 供追溯，进包文件使用规范名。

| 上传返回（退出码） | 含义 | 处理 |
|---|---|---|
| 状态 | 含义 | 处理 |
|---|---|---|
| 报告存在 | 本地评审报告已生成 | 复制到预检包，包内命名为 `<KEY>-需求评审报告-宋立志.md` |
| 报告不存在 | 五步流水线或报告生成失败 | 记「未生成（原因）」，继续后续评审与本地打包 |

### 11.1 原型设计准入报告（review-jira-design）

**前置判定（强制）**：先按 §3.1 运行 `detect-prototype.mjs`。只有 `hasPrototype=true`（或确认 `ambiguous` 里的文件确实是原型）时，才在需求质量评审（§11）跑完后、QA 阶段一（§11.2）之前调用整合包内的 `review-jira-design`：对照定稿 PRD 审核产品原型初稿，检查需求完整性、PRD—原型一致性、流程与状态覆盖。它**对 Jira 只读**，不发起任何网络写请求，输入只有本地 PRD 与原型文件。**单子未上传原型时本节整体跳过**：不调用该技能、不生成设计报告，本地草稿记「不适用（本单未上传原型）」，继续 §11.2 与后续交付。

**技能位置**：本整合包内固定为 `<技能目录>/subskills/review-jira-design`。读取该子技能的检查项与报告流程时，**把它文档中的默认技能目录替换为本包路径**，不要访问 `/data/skills/review-jira-design`。严格按它的步骤、检查项与结论口径执行；不要改动它的脚本与报告模板。

**输入取自本单附件**：

- **定稿 PRD**：§3 已下载的附件里版本唯一可定位的那份（如 `work/jira-gate/<KEY>/attachments/PRD_DELIVERY.md` 或解压后的 `.../__unpacked/附件.rar/xxx_PRD_DELIVERY.md`）。存在多个候选时按需求单描述锁定唯一对象，**不要按文件名相似度自行挑选**。
- **产品原型初稿**：附件里的业务 HTML（如 `work/jira-gate/<KEY>/attachments/Prototype.html`）及其必要资源；仅有原型图片时以图片为输入并注明「只能核对图片可见信息」；有多个原型时以需求单描述指定的入口为准。**原型必须来自本单实际上传的附件（含解压产物）**，不得引用其它单子或历史版本。
- 已判定有原型、但 PRD 缺失或原型无法唯一定位/无法读取时，不要跳过本步骤、也不要自造「通过」：照常形成「审核未完成」结论并写明缺什么；如果连 `build-report.mjs` 的 `--prd` 输入都不存在，无法生成 HTML，则在本地草稿里记录「设计审核未完成（缺少 PRD）」并省略设计报告文件，不得伪造报告。**复核后若发现命中的只是历史 `*原型设计准入*.html`、QA 清单、评审报告等自生成产物，说明本单实际没有业务原型，必须回到 §3.1 按「不适用」处理**，不得生成设计报告，也不得把「缺少业务原型」写成「审核未完成」。

**固定执行顺序**（报告落在 G1 本次任务目录内，便于 §12 统一清理）：

```bash
export DESIGN_SKILL_DIR="<技能目录>/subskills/review-jira-design"
DESIGN_OUT="$PWD/work/jira-gate/<KEY>/design"
mkdir -p "$DESIGN_OUT"
node "$DESIGN_SKILL_DIR/scripts/build-report.mjs" --prd "<PRD绝对路径>" --dump-index
# 按 references/content-example.json 写 <DESIGN_OUT>/content.json（只写模型判定，不手写 HTML）
node "$DESIGN_SKILL_DIR/scripts/build-report.mjs" --prd "<PRD绝对路径>" \
  --content "$DESIGN_OUT/content.json" --out "$DESIGN_OUT/<KEY>-原型设计准入报告.html"
```

**结果读取**：设计结论只从 `<DESIGN_OUT>/content.json` 的 `结论.审核结论.值` 读取，取值只能是 `通过` / `有条件通过` / `退回补齐` / `审核未完成`；同时读取 `汇总条` 与问题区的级别计数，用于本地草稿摘要。不许根据报告标题或问题数量自己猜结论。

**证据口径**：本版本**不采集截图**，只依据 PRD 原文、原型源码与页面结构核对；未实际运行、内网资源不可达或无法判断的内容标记为「未验证」，**不要伪造截图或运行证据，也不要安装浏览器**。

**合并规则（硬约束）**：

- `不适用`（本单未上传原型）：设计准出不阻断，G1 保持 §6 自身结论；草稿写「原型设计准入：不适用（本单未上传原型）」「设计报告附件：不适用（本单未上传原型）」，不要写成「未上传（原因）」这种失败口径。
- `有条件通过`：非阻断事项不改变 G1 结论，但必须在草稿的「原型设计准入」行注明「有条件通过（N 项待补齐）」。
- `退回补齐`：G1 最终结论强制为**退回补充**，草稿建议流转到「需求设计」。
- `审核未完成`：不得视为通过，G1 最终结论强制为**退回补充**；在草稿里写明缺什么（PRD / 原型 / 对象无法唯一定位 / 证据不足），设计报告能生成就进包，不能生成就省略并说明原因。
- `build-report.mjs` 非零退出、内容 JSON 非法、报告未生成：属于运营性失败，设计准入记为「未完成（原因）」，报告写「未生成（原因）」，**不改变 G1 自身结论，也不中断 G1**；继续执行 `review-jira-qa`、写本地草稿并打包已生成的报告。不得把脚本故障写成设计阻断项。
- 仅在已生成设计报告时：HTML 进本地预检包，包内文件名固定为 `<KEY>-原型设计准入报告.html`；不检查上传权限，不调用 `attach` / `attach-prune` / `attach-list`。打包脚本会读回文件并校验包内条目。
- **本次判定为「不适用」（未上传原型）时**：不生成新设计报告，也不清理 Jira 历史附件；草稿固定写「设计报告附件：不适用（本单未上传原型）」。
- 设计技能自身不改 Jira 状态；本技能也不执行流转，只写预演建议。

### 11.2 QA 需求准入门禁（review-jira-qa）

G1 还要调用整合包内的 `review-jira-qa` 技能执行 **阶段一 QA 需求准入门禁**。它按 B1–B7 / C1–C6 独立判定，产出需求准入门禁清单 HTML 并输出机器可读 result。**只执行阶段一**，禁止进入测试方案、XMind、Excel、Xray CSV 等阶段二/三。

**技能位置**：本整合包内固定为 `<技能目录>/subskills/review-jira-qa`。读取该子技能的规则与流程时，**把它文档中的默认技能目录替换为本包路径**，不要访问 `/data/skills/review-jira-qa`。严格按其中阶段一流程执行，不要改动它的判定标准。该副本的传输层已启用本地预检硬边界：Jira 只允许 GET；任何评论、附件写入或删除请求都会被拒绝，即使命令漏写 `--dry-run` 也不会提交。

**环境与工作区**：QA 工作区必须放在 G1 本次任务目录内，这样 §12 的清理能一并收尾；不得指向技能安装目录：

```bash
export SKILL_DIR="<技能目录>/subskills/review-jira-qa"
export CLAUDE_SKILL_DIR="$SKILL_DIR"
export JIRA_PAT="${JIRA_PAT:-$JIRA_TOKEN}"
export JIRA_BASE_URL="${JIRA_BASE_URL:-https://jira.in.wezhuiyi.com}"
export QA_WORKSPACE="$PWD/work/jira-gate/<KEY>/review-jira-qa"
```

**固定执行顺序**（0.1.18 起用 `prepare.mjs --stage 1` 一次完成准备步骤；`<runId>`、`<digest>`、`<coverageRequired>` 取 `prepare` 输出；`<gate.spec.json>`、`<HTML>`、`<COMMENT>` 按脚本实际输出路径替换）：

```bash
node "$SKILL_DIR/bin/prepare.mjs" --key <KEY> --stage 1
# prepare 内部依次执行 preflight → jira-read --comments 10 → jira-download → reuse.mjs → materials.mjs → coverage.mjs
# 读 prepare 输出的 status：
#   READY   → 继续；记下 runId、digest、coverageRequired、requiredAcs、suggestedVersion
#   REUSED  → 直接复用上一版结论，零写入，不执行后续步骤，继续 G1 交付
#   SKIPPED → QA 记为「不适用」，不生成 HTML、不评论，继续 G1 交付
#   ERROR   → QA 记为「未完成」，停止 QA 剩余步骤，继续 G1 交付
# READY 时读 prompts/stage1-gate.md 与 digest 指向的 materials-digest.md，完整核读全部需求侧材料
# 对 materials.json 中全部 active 需求侧材料做结构扫描（extractedPath 优先，否则 localFile）
node "$SKILL_DIR/bin/scan.mjs" --source <材料文件1> --source <材料文件2> ... --out ".qa/runs/<runId>/candidates.json"
# 写 gate.spec.json：checks 必须覆盖 B1-B7/C1-C6 共 13 项；coverage 覆盖全部 AC；
# failures 带 facts[].quote 与 criteria；scans 逐条回应 candidates；不手写 HTML
node "$SKILL_DIR/bin/render-gate.mjs" --spec <gate.spec.json>
node "$SKILL_DIR/bin/validate-gate.mjs" --html <HTML> --spec <gate.spec.json> \
  --coverage-required ".qa/runs/<runId>/coverage-required.json" \
  --candidates ".qa/runs/<runId>/candidates.json" \
  --source <材料文件1> --source <材料文件2> ...
node "$SKILL_DIR/bin/jira-upload.mjs" --key <KEY> --file <HTML> --run-id <runId> --stage risk-analysis --dry-run
node "$SKILL_DIR/bin/jira-comment.mjs" --key <KEY> --body <COMMENT> --run-id <runId> --stage risk-analysis --dry-run
node "$SKILL_DIR/bin/result.mjs" --stage risk-analysis --key <KEY> --run-id <runId> --spec <gate.spec.json>
```

**执行与结果收口**：不要用 `set -e`，命令逐条执行。`review-jira-qa` 的所有脚本统一以 exit 0 结束，是否继续只看 `status` / `applicable` / `ok`，不能把退出码当成成功依据。

- `prepare.mjs` 输出 `status=REUSED`（内部 `reuse.mjs` 的 `REUSE=true`）：当前材料附件 ID 集合与上一版门禁一致，QA 直接复用上一版结论。读取 `prepare` 输出的 `conclusion`、`suggestedVersion`、`commentId`，以及 `reuse.json` 与 `QA_RESULT_JSON`，把上一版结论计入 G1 合并规则；**不重新分析、不新建附件或评论、不执行 QA 后续步骤**，继续本地交付。只有显式 `--force` 才重新评审。
- `reuse.mjs` 失败时 `prepare` 会整体返回 `status=ERROR` / `ok=false` 或带 `warnings`；按 QA 运营性失败记为「未完成（原因）」，继续执行 G1 后续步骤；不得假定复用成功，也不得中断 G1。
- `prepare.mjs` 输出 `status=SKIPPED`（内部 `jira-download` 判定输入不适用）：QA 记为「不适用（reason/errorCode）」，不再生成 HTML、不写门禁评论，继续本地交付。
- 任一脚本输出 `status=ERROR` 或 `ok=false`：停止 QA 剩余依赖步骤，把 QA 记为「未完成（status/errorCode/error）」，继续 G1 后续交付；不要等待人工确认。
- `result.json` 与 `QA_RESULT_JSON` 都缺失：按 `status=ERROR`、`errorCode=INTERNAL`、「QA 未完成（无结果）」处理并继续 G1。

**结果读取**：以 `QA_WORKSPACE/.qa/runs/<runId>/result.json` 为准，至少读取：

- `ok`、`status`（正常完成 / `ERROR` / `SKIPPED`）、`applicable`、`conclusion`（`通过` / `暂不通过`）、`failedItemCount`、`errorCode`、`error`；
- `htmlPath`；
- `htmlPath`（进包前复制为 `<KEY>-<需求名称>-需求准入门禁清单-v<版本>.html`，以 `upload.json.files[0].filename` 或 render 输出为准）；
- `reused`（是否复用上一版）、`deliverableVersion`、`materialAttachmentIds`（来自 `reuse.json` 或 `result.json`）；复用时不要求新的 `htmlPath`。

`status=REUSED` 时不会重跑阶段一，因此以 `prepare.mjs` 输出、`QA_WORKSPACE/.qa/runs/<runId>/reuse.json` 和 `reuse.mjs` 输出的 `QA_RESULT_JSON` 为准：读取上一版 `conclusion`、`suggestedVersion`（或 `deliverableVersion`）、`commentId`，并据此执行合并规则；此时 `result.json` 缺失是正常现象，**不得判为 QA 未完成或运营性失败**。

**合并规则（硬约束）**：

- `ok=true` + `conclusion=通过`：QA 准出不阻断，G1 保持 §6 自身结论。
- `ok=true` + `conclusion=暂不通过`：G1 最终结论强制为**退回补充**，按 §9 建议流转到「需求设计」；QA 的未关闭项计入本地草稿的「QA 阻断项」。
- `status=SKIPPED` / `applicable=false`：QA 记为「不适用（reason/errorCode）」，**不得写成 QA 通过，也不得包装成业务阻断项**；清单写「未生成」，继续完成 G1 本地草稿和打包。
- `status=ERROR`、`ok=false` 或 `result.json` 缺失：属于运营性失败，**不得写成 QA 通过，也不得包装成业务阻断项**。把 QA 写成「未完成（status/errorCode/error）」，清单写「未生成」，然后**继续执行 G1 的本地草稿和打包**；不要停在 QA 技能里。
- QA 结论为「暂不通过」仍是业务完成（退出码 0），必须继续完成 G1 草稿，不能因红叉而中断。
- QA 只生成本地 HTML，不写 Jira 评论、不改 Jira 状态。`jira-upload` 与 `jira-comment` 都带 `--dry-run`，只做本地运行态记录；不要读取它们的 attachmentId/commentId 当作 Jira 交付。
- 若 QA 结果文件与 `QA_RESULT_JSON` 都缺失，按「QA 未完成（无结果，status=ERROR）」处理并继续 G1。QA 的任何失败（脚本异常、超时、结果缺失、JSON 非法）都不得中断 G1；只有本地打包动作在重试后仍失败时，才保留现场。

### 写机器可读结果（清理前必做）

本技能是平台 `/skill/run` 的顶层技能，必须在全部交付动作完成、清理工作区之前，把最终结果写到**任务根目录**（当前工作目录）的 `.skill-result.json`：

```json
{
  "schemaVersion": 1,
  "skillId": "jira-gate-1-local",
  "issueKey": "CALL-1941",
  "conclusion": "通过",
  "summary": "本地预检：G1、原型设计准入（不适用：本单未上传原型）与 QA 均通过，建议保持状态，已生成预检包",
  "blockingCount": 0
}
```

- `conclusion` 只能写最终业务结论：`通过` 或 `退回补充`；类型不是「故事」或产品归属不在清单内而提前结束时写 `不适用`。
- `summary` 用一句话写清 G1、设计准入（有原型时写结论，无原型时写「不适用：本单未上传原型」）、QA 三条准出和状态流转结果，不超过 500 字；`blockingCount` 是最终阻断项数量（没有阻断项写 `0`）。
- 子技能的运营性失败不要伪造「通过」，也不要把技术错误写成业务阻断；G1 仍应写该文件，结论取 G1 Checklist 与各子技能**业务结论**的合并结果，并在 `summary` 中注明「未完成」的子技能与原因。
- 只有读取 Jira 失败、无法形成业务结论时，才可以不写该文件；平台会把结论记为未知。
- 该文件只用于调用统计，不是交付物，不写入 Jira；平台读取入库后会自动删除，不要放进 `work/`、`reports/`、`outputs/` 或压缩包。
- 调用 `review-jira-req`、`review-jira-design`（仅 `hasPrototype=true` 时）、`review-jira-qa` 时，必须在子任务指令里明确「不要写任务根目录 `.skill-result.json`」，避免把子技能误计为独立调用。

### 12. 打包交付并清理本次任务的工作区中间产物

全部本地交付物就绪后，用打包脚本生成唯一预检包；打包成功后再把中间产物删掉，别让附件副本按任务堆积：

```bash
node <技能目录>/scripts/pack-local-deliverables.mjs --key <KEY> \
  --comment work/jira-gate/<KEY>/comment.txt \
  --report "reports/review-report-<时间戳>.md"
# 有原型时追加：--design "work/jira-gate/<KEY>/design/<KEY>-原型设计准入报告.html"
# 有 QA 清单时追加：--qa "work/jira-gate/<KEY>/review-jira-qa/.qa/runs/<runId>/<QA清单>.html"
```

- 包内文件固定为 `<KEY>-G1预检评论.txt`、`<KEY>-需求评审报告-宋立志.md`、可选 `<KEY>-原型设计准入报告.html` 与可选 `<KEY>-<需求名称>-需求准入门禁清单-v<版本>.html`。
- 输出默认在 `<工作目录>/outputs/<KEY>-G1本地预检包-<时间戳>.zip`；读回 JSON 的 `ok=true`、`zipBytes>0` 和 `entries` 后才能宣布完成。
- 缺少评审报告时改用 `--no-report`，草稿必须如实记录「未生成（原因）」；QA 清单和设计报告本来就可选，不要传占位文件。

```bash
node <技能目录>/scripts/workdir-cleanup.mjs --key <KEY>
```

- 清理范围只有 `<工作目录>/work/jira-gate/<KEY>/` 一个目录（附件、`__unpacked/` 解压产物、`attachments.txt`、`comment.txt`、`gate.json`、`issue.md`、`design/` 下的设计内容 JSON 与报告 HTML，以及 `review-jira-qa/` 下的 QA 工作区与运行态）。
- **`outputs/` 不在清理范围内**：预检包是唯一交付物。`reports/` 中的原始报告也保留供追溯，进包文件使用规范名。
- 脚本按单号定位目录（单号不区分大小写），解析后的父目录必须正好是 `work/jira-gate`，否则拒绝执行——路径写错时不会误删工作区。
- 收尾是幂等的：目录已经不存在时返回 `{ok:true, removed:false, reason:"not-found"}`，不算失败。
- 想先看看会删掉什么，加 `--dry-run`：只统计文件数与字节数，不实际删除。
- 子技能失败但本地预检包已完成时，仍要调用本脚本正常收尾。只有打包动作异常中断、需要保留现场排查时，才不要调用本脚本。

```json
// 输出示例
{ "ok": true, "key": "CALL-1941", "removed": true, "removedFileCount": 12, "removedBytes": 4781234 }
```

## 输出模板（本地评论草稿）

结论只进本地预检包；需求质量的细读结论见包内评审报告，原型设计准入结论见包内设计报告（单子未上传原型时为「不适用」，没有该文件），QA 准入门禁的完整清单见包内 QA HTML。通过与否结构相同，只在标题措辞、每项标记与阻断说明上不同。

**通用结构**

```text
h3. G1 需求准入结论（本地预检）：{{通过（允许进入开发设计中） / 退回补充（建议流转至需求设计）}}

* 结论：{{通过 / 退回补充}}
* 结论说明：{{一句话}}
* PRD：{{精确路径}}，版本：{{版本}}
* 原型设计准入：{{通过 / 有条件通过（N 项待补齐） / 退回补齐 / 审核未完成（原因） / 未完成（原因） / 不适用（本单未上传原型）}}
* 设计报告附件：{{<KEY>-原型设计准入报告.html（已在预检包） / 未生成（原因） / 不适用（本单未上传原型）}}
* QA 准入门禁：{{通过 / 暂不通过（N 项未关闭） / 未完成（原因）}}
* QA 阻断项：{{0 / N 项}}
* QA 清单附件：{{<KEY>-<需求名称>-需求准入门禁清单-v<版本>.html（已在预检包） / 未生成（原因）}}
* QA 评论：未写入（本地预检）
* 业务待决策事项：{{无；或见下方最多 5 项}}
* 架构师澄清：{{数量；G2 前关闭时点}}
* 研发技术澄清：{{数量；G2 前关闭时点}}
* 复审范围：{{全量 / 受影响章节}}
* 产品架构师确认人／时间：{{待确认}}
* 预检时间：{{时间}}（AI 预检，最终以产品架构师确认为准）
* 门禁字段：预演未写入
* 状态流转：预演未执行（建议：{{保持当前状态 / 流转至需求设计}}）
* 建议处理人：{{[~登录名]}}

h4. MUST（8 项）
# (/) *5.1 事实源*（MUST）
# (x) *5.2 目标与范围*（MUST）：{{问题}} → {{要补什么}}；证据：{{位置}}
# (/) *5.3 核心链路*（MUST）
# (/) *5.4 业务规则*（MUST）
# (/) *5.5 状态与权限*（MUST）
# (/) *5.6 业务数据*（MUST）
# (/) *5.7 异常恢复*（MUST）
# (/) *5.8 验收标准*（MUST）

h4. CONDITIONAL（命中场景逐条判定，未命中记 N/A）
# (/) *6.1.1 UI 与字段-业务契约*（CONDITIONAL，N/A）：{{未触发依据}}
# (/) *6.1.2 UI 与字段-交互结果*（CONDITIONAL，N/A）：{{未触发依据}}
# (/) *6.2.1 角色/状态-可见与可执行范围*（CONDITIONAL，N/A）：{{未触发依据}}
# (/) *6.2.2 角色/状态-高风险确认*（CONDITIONAL，N/A）：{{未触发依据}}
# (/) *6.3.1 跨系统-责任边界与结果*（CONDITIONAL，N/A）：{{未触发依据}}
# (/) *6.3.2 跨系统-异步与失败恢复*（CONDITIONAL，N/A）：{{未触发依据}}
# (/) *6.4.1 迁移/兼容-新旧行为*（CONDITIONAL，N/A）：{{未触发依据}}
# (/) *6.4.2 迁移/兼容-回退与不可逆*（CONDITIONAL，N/A）：{{未触发依据}}
# (/) *6.5.1 LLM-业务质量与门槛*（CONDITIONAL，N/A）：{{未触发依据}}
# (/) *6.5.2 LLM-失败策略与副作用*（CONDITIONAL，N/A）：{{未触发依据}}
# (/) *6.6.1 性能/安全/隐私-口径与超限*（CONDITIONAL，N/A）：{{未触发依据}}
# (/) *6.6.2 性能/安全/隐私-敏感数据*（CONDITIONAL，N/A）：{{未触发依据}}

h4. SHOULD（不阻断 G1，记录取舍）
# (/) *7.1.1 追溯矩阵*（SHOULD）
# (/) *7.1.2 决策表*（SHOULD）
# (/) *7.1.3 参考资料用途*（SHOULD）
# (/) *7.1.4 代表性反例*（SHOULD）

* 报告附件：{{<KEY>-需求评审报告-宋立志.md}}（{{已在预检包 / 未生成（原因）}}）｜评审结论：{{不合格 / 需修改 / 合格 / 未完成（原因）}}
```

「建议处理人」这一行**只在不达标时出现**：`{{[~登录名]}}` 是 §9 取到的处理人文本，不是真实 @。结论「通过」时整行省略。

「报告附件 + 评审结论」是**评论末尾固定的一行，达标与不达标都要有**，位置在检查项清单之后（末尾），用来让人从评论直接定位到报告、并一眼看到质量结论：

- 附件名按 §11 的规则可提前确定；`评审结论`取三档之一——**不合格 / 需修改 / 合格**，只能来自报告 md，不能自己判；报告未生成或提取失败时写 **未完成（原因）**，仍继续交付。
- 因为末尾要写这个结论，**五步流水线必须在写评论之前跑到第 5 步**（§11），拿到 md 再用 `report-conclusion.mjs` 提取；不许先猜一个值写进评论。报告未生成或提取失败时写「未完成（原因）」，并继续完成 G1 评论，不得因此中断。
- 报告生成失败时写「未生成（原因）」；生成成功时必须进入预检包，并把括号写成「已在预检包」。

**退回补充时的业务待决策事项**（最多 5 项，按「目标范围、核心链路、规则状态、异常恢复、验收」归并；不得出现 Java 类、接口设计、数据库类型、SQL、缓存 Key、Topic、线程池、部署参数、组件选型、模型 Schema 或环境配置）：

```text
h5. 业务待决策事项 1：{{业务场景}}
* 当前歧义：{{两种都合理的解释}}
* 两种可能结果及影响：{{A / B}}
* 推荐方案及依据：{{推荐 A，因为…}}
* 关联：{{FR/BR/IR/AC 或 PRD 章节}}
```

**标记规则**

| 情形 | 标记 |
|---|---|
| 该检查项满足 | `(/)` 绿勾 |
| 该检查项不满足 | `(x)` 红叉 |
| 场景项未触发（N/A） | `(/)` 绿勾 + `N/A` 与依据 |

- 每个检查项**必须**有标记，满足项也要写 `(/)`，不要省略。
- 只有不满足项才写「问题 → 要补什么；证据」；满足项只写编号与名称。
- `SHOULD` 不阻断 G1，但仍按实际情况打标记，并在括号里注明「SHOULD，不阻断 G1」。

**示例（退回补充，节选）**

```text
h3. G1 需求准入结论：退回补充（流转至需求设计）

* 结论：退回补充
* 结论说明：核心权限与异常恢复未闭环，研发需要自行选择产品规则
* PRD：需求附件/PRD_DELIVERY.md，版本：v1.0
* 原型设计准入：退回补齐（2 项阻断）
* 设计报告附件：CALL-1941-原型设计准入报告.html（已在预检包）
* QA 准入门禁：暂不通过（2 项未关闭）
* QA 阻断项：2 项
* QA 清单附件：CALL-1941-需求准入门禁清单-v1.0.html（已在预检包）
* QA 评论：未写入（本地预检）
* 业务待决策事项：2 项（见下）
* 架构师澄清：0 项
* 研发技术澄清：1 项（Schema 由研发补充，不阻断 G1）
* 复审范围：全量
* 产品架构师确认人／时间：待确认
* 预检时间：2026-10-22 17:30（AI 预检，最终以产品架构师确认为准）
* 门禁字段：预演未写入
* 状态流转：预演未执行（建议：流转至需求设计）
* 建议处理人：[~liu.huan]

h4. MUST（8 项）
# (/) *5.1 事实源*（MUST）
# (x) *5.2 目标与范围*（MUST）：未写本期不包含范围 → 补充相邻范围与排除项；证据：PRD §2
# (/) *5.3 核心链路*（MUST）
# (/) *5.4 业务规则*（MUST）
# (x) *5.5 状态与权限*（MUST）：未说明审批角色能否查看草稿 → 补角色×状态可见性；证据：PRD §4.2
# (/) *5.6 业务数据*（MUST）
# (x) *5.7 异常恢复*（MUST）：提交失败后数据是否保留未说明 → 补失败反馈与恢复方式；证据：PRD §5
# (x) *5.8 验收标准*（MUST）：P0 主流程缺少可判断 AC → 补主流程与关键失败 AC；证据：PRD §7

h4. CONDITIONAL（命中场景逐条判定，未命中记 N/A）
# (x) *6.2.1 角色/状态-可见与可执行范围*（CONDITIONAL）：未给出角色×状态矩阵 → 补可见与可执行范围；证据：PRD §4.2
# (x) *6.2.2 角色/状态-高风险确认*（CONDITIONAL）：驳回后能否再次提交未说明 → 补确认后的状态与反馈；证据：PRD §4.3
# (/) *6.5.1 LLM-业务质量与门槛*（CONDITIONAL）：门槛与失败行为已写明

* 报告附件：CALL-1941-需求评审报告-宋立志.md（已在预检包）｜评审结论：需修改
```

**复审（同一单第二次及以后）**

沿用同一模板，标题改为「G1 需求准入结论（本地预检）（复审）：{{通过 / 退回补充}}」，生成新的本地复审草稿，不修改或回复 Jira 历史评论。检查项清单按最新判定重新打标记：本次已补的项由 `(x)` 变为 `(/)`，仍缺的项保持 `(x)`。

**Jira wiki 标记对照**（写评论时按此转换，不要提交 Markdown）

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

## 预检规范

**取数范围**（以 Jira 需求单为唯一入口）

- 主单字段：类型（**必须是「故事」**）、概要、描述、优先级、客户名称、产品归属、影响的版本、经办人、标签、模块、工时 / Story Points。这些字段是**定位与证据来源**，字段缺失本身不构成不达标。
- 附件：PRD（含版本）、原型、截图、数据样例；压缩包需解压后逐项阅读。
- 问题链接：前置、阻塞、被阻塞、关联需求。
- 子任务与子任务状态。
- 历史评论与改动记录：判断"待确认"是否已被回答、PRD 是否已升版。

**证据要求**

- 每条判定必须指向**具体证据位置**：PRD 章节、字段名、描述段落、附件文件名（必要时页码或行号）、评论时间点。
- 不允许无证据的"已满足"；找不到证据一律记为"不满足"。
- 证据冲突时（描述与附件结论不一致）记为"需要产品确认"，不自行选择其一。
- 附件位于压缩包内时，证据写到内部路径，例如 `附件.rar/xxx_PRD_DELIVERY.md §5.2`。

**禁止事项**

- 不把"检索工作"反问给产品经理；不因为某项无法判定而停止其余项的检查。
- 不编造需求未写明的内容，不把推测写成已确认事实。
- 不把纯技术实现细节、单据字段缺失、测试数据未准备包装成 G1 阻断项。
- 不因难度或工作量而放宽 `MUST` 与 `CONDITIONAL`。
- 不擅自修改需求单字段、不指派经办人；建议处理人只作为本地草稿文本。
- 不 @ 无关人员：`[~登录名]` 只作为「建议处理人」文本写入草稿，不真实 @。
- **唯一交付物是本地预检包**：评论草稿、需求评审报告、设计报告（仅有原型时）与 QA 清单只保留在本地，不上传 Jira，也不在需求单中留下草稿。

## 令牌配置

凭证与地址解析顺序：容器内由后端注入 `SKILLRUN_ENV_LOCKED=1` 时**只认环境变量**（`JIRA_TOKEN`、`JIRA_BASE_URL`），不接受 `--token` / `--base-url` 与令牌文件，访问其它站点会直接报错；桌面端未注入该标记时按 入参 → 环境变量 → 令牌文件 解析。令牌文件按序查找 skill 目录下的 `.jira-token` / `.jira-token.txt` / `jira-token.txt`、`~/.codex/jira-token.txt`、`~/.codex/jira/config.json`（支持 `{"token":"...","baseUrl":"..."}`）。

`selftest()` 返回的 `baseUrl` 与 `baseUrlSource` 会标明本次实际访问的站点及来源；**若 `baseUrlSource` 是「内置默认值（生产 Jira）」而当前并非生产环境，先停下来修配置，不要继续预检。**

- 令牌来源：Jira → 右上角头像 → 个人访问令牌 → 创建令牌（Jira 10.3 Server 已启用 PAT）。
- 令牌只用于 `Authorization: Bearer`，不要打印、回显或写进报告文件。
- `selftest()` 返回未找到令牌时，直接告诉用户按上面的路径放好令牌文件，并说明放好后可以立刻重试；不要改用浏览器提交任何 Jira 内容。

## 硬性约束

- **只处理类型为「故事」且产品归属属于 Bot/Call/See/Pal/Learn/Aiforce 之一的单子**；任一项不符时立即停止，不做任何 Jira 写操作，也不调用 `review-jira-req`、`review-jira-design` 或 `review-jira-qa`。
- 状态流转规则：**综合结论通过建议保持当前状态**；**综合结论退回补充建议 → 需求设计**；不得建议流转到「评审中」「待办」「开发中」等其他状态。不指派经办人，不调用流转接口。
- **最终结论必须合并准出**：G1 Checklist、`review-jira-design`（**仅单子已上传原型时参与**；通过 / 有条件通过才算通过，未上传原型记「不适用」不参与判定）与 `review-jira-qa` 阶段一的任一**业务不通过**，最终结论即为「退回补充」。子技能的任何运营性失败都只记为「未完成（原因）」，不得写成通过或业务阻断项，也不得中断 G1 后续交付。
- **禁止调用 `gate-set` / `setGateResult()`**；门禁字段只允许 `gate-get` 只读对照，不得写 `G1需求难度`。
- **报告和评论只生成本地交付物**：§11 的评审报告 md、§11.1 的设计报告 HTML（仅单子已上传原型时生成）与 §11.2 的 QA 清单 HTML 都必须进预检包；判定结果不得写入 Jira。
- 不编造需求未写明的内容，不把推测写成已确认事实。
- 不把单据字段缺失、测试数据未准备、纯技术细节当作不达标理由。
- 不留下未完成的草稿评论；评论草稿只落 `work/jira-gate/<KEY>/comment.txt`。
- **禁止调用 `comment`、`comment-update`、`attach`、`attach-prune`、`transition` 等写命令**；只读取单据、附件、历史评论、门禁字段与流转元数据。包内 QA 子技能的 Jira 传输层同样只允许 GET，所有 POST / PUT / DELETE 写请求都会被硬拦截。
- 同一需求只保留最新一个预检包：打包成功后清理 `outputs/<KEY>-G1本地预检包-*.zip` 旧包，但不得触碰 Jira 附件和历史评论。
- 交付全部完成后先写任务根目录 `.skill-result.json`（见「写机器可读结果」），再按 §12 清理本次任务的工作区中间产物（只删 `work/jira-gate/<KEY>/`）。子技能失败但本地预检包已完成时仍要写结果并清理；只有打包动作异常中断时才保留现场。
