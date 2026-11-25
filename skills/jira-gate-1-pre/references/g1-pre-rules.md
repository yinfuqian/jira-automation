# G1-Pre 上游需求流转门禁规则（V1.2，2026-11-25 版）

> 本文件是 `jira-gate-1-pre` 的判定依据，内容对应《GENERIC_TEAM_G1_PRE_SKILLS》V1.2
> （contractVersion `1.0`、tools_version `V2.3.1`、created_at `2026-11-17`、updated_at `2026-11-25`）。
> 判定时以本文件为准；不要凭印象发明检查项或放宽口径。

## 0. 本仓库落地差异

规范假设存在 Jira 接入层在 transition 前调用本 Skill，并由接入层决定是否放行。本仓库没有该拦截层，
因此按以下方式落地，其余规则与上游规范一致：

- **本技能不执行 transition**：只输出 `conclusion` 与评论，调用方拿到结论后自行决定是否继续原 transition。
- **评论由本技能写入**：`dry_run=true` 时只返回拟写评论，不写 Jira。
- **无接入层传入 transition**：按问题类型推断预期目标状态，并记录 `transition.provided=false`；
  transition 上下文只保存在 `result.json` 与任务元数据中，绝不据此执行流转。
- **置顶由接入层负责**：本技能不调用置顶接口，`result.json` 中 `pinStatus` 固定为 `pending`。
- **幂等通过比对最新评论正文**：评论中不写规则版本、内容哈希或 sha256；本轮渲染结果与最新一条
  G1-Pre 评论正文一致时不重复评论。
- **`not_applicable` 不写评论**：`applicability=not_applicable` 时只返回分流理由，不写门禁评论、不写内容结论。
- **执行异常不写内容结论评论**：`executionStatus=execution_error` 时不写「通过 / 不通过」评论，
  由平台记录并重试，不允许静默放行。
- **类型过滤**：只认 Jira 的 `issuetype`，五种类型之外直接跳过；不要用标题、标签或描述猜测类型。
- **需求设计后的内部流转不重复检查**：`客户用户故事` / `故事` 在显式传入目标状态 `评审中` 时直接跳过。

---

## 1. 定位

本 Skill 是 Jira 需求进入下一状态前的 G1-Pre 向下流转门禁，只由 Jira 接入层调用。

它解决的问题是：

> 上游提交人是否已经把需求对象、当前问题、诉求、期望和必要证据说清楚，使下一接手方能够继续处理？

它负责：

- 读取标题、描述、结构化字段、当前附件、外部链接、评论及其作者和时间。
- 区分原始需求、补充事实、技术答复、决策记录、历史评审和其他参考材料。
- 判断需求表达是否足够清楚、材料是否可核验、是否存在同层事实冲突。
- 对需求事项只输出 `通过` / `不通过`；对不是需求的事务性事项返回 `not_applicable`，不生成内容结论。
- 不通过时先列出 N 个阻断项，再逐项给出当前问题、依据和最小补充动作。
- 通过时生成并置顶「需求评估摘要」，帮助下一接手方快速理解，但不替其形成方案。
- 记录规则版本、契约版本、内容哈希、材料来源、运行标识和检查证据。

它不负责：

- 判断需求应由产品、研发、测试、算法、运维还是项目管理接手。
- 替接手方形成产品方案、技术方案、业务规则、最终范围或验收标准。
- 判断需求是否已经达到 PRD、技术方案、测试方案或完整验收标准。
- 因为产品决策或技术方案尚未完成而阻断向下流转。
- 要求上游提交人代替研发、测试、算法或产品经理回答下游专业问题。
- 直接修改 Jira 状态、经办人、优先级、产品归属、截止日期、Story Points 或其他字段。
- 根据历史 Issue Key、经办人或项目名称设置白名单和特例。

结论定义：

| 结论 | 含义 |
|---|---|
| `通过` | 下一接手方能够理解发生了什么、希望改变或获得什么、依据是什么，以及还有哪些问题需要继续澄清；允许进入下一状态，并生成需求评估摘要。 |
| `不通过` | 需求存在关键事实缺口、核心事实冲突或必需证据不可用；终止本次 transition，退回补充或修复。 |

核心边界：

- **可以继续追问**不等于「可以流转」；如果关键事实缺失会让接手方无法理解要处理什么，应不通过。
- **方案尚未决定**不等于「需求不清」；只要来源、差异和待决问题已明确，应通过并写入建议澄清方向。
- **表达是否专业**不是门禁目标；门禁只要求业务事实可理解、诉求可识别、证据可核验、差异不隐藏。
- **是否使用模板**不是门禁目标；模板用于帮助表达，不是唯一合法格式。

---

## 2. 调用边界

### 2.1 触发点

Jira 接入层在用户触发 transition 后、状态实际变更前调用本 Skill：

| Jira 问题类型 | 触发动作 | 允许继续的目标状态 |
|---|---|---|
| `客户用户故事` | `跟进需求` | `需求澄清` |
| `故事` | `跟进需求` | `需求澄清` |
| `项目概算评估` | `开始评估` | `处理中` |
| `任务` | `开始进行` | `处理中` |
| `项目实施阶段` | `开始评估` | `处理中` |

transition 名称或目标状态与当前 Jira 配置不一致时，以 Jira 实际 transition ID 为准；接入层必须把实际 ID、
源状态、目标状态和源经办人传给 Skill。

本 Skill 不定义后续工作流，但接入层必须遵守：

- 通过：允许原 transition 继续，写入并置顶需求评估摘要。
- 不通过：取消原 transition，保留或按工作流打回「待办」，并回退到发起门禁前的经办人。
- 执行异常：不生成内容通过/不通过，保持源状态和经办人并重试。

Skill 不直接写 Jira 状态、经办人和评论；状态、经办人和评论写入均由接入层执行。

> **本仓库落地**：没有接入层，评论由本技能自己写入，状态与经办人一律不碰。

### 2.2 不调用范围

以下事项默认不调用 G1-Pre，因为其不是待流转的需求：

- 会议、出差、考勤、工时、培训和其他事务性记录。
- 已由系统规则明确识别为版本发布、环境运维或批量生成，且正文没有可识别的需求诉求。
- 没有对象、问题或诉求的空任务。

以下内容只要形成了需求并触发配置的 transition，就必须调用同一套 G1-Pre 判定：

- 功能新增、优化、规则调整和交互变化；
- Bug、运行异常、AI/LLM 效果问题和质量不达标；
- 非功能要求、性能容量评估、技术可行性和资源评估；
- 迁移、适配、压测和兼容性支持；
- 文档整理、研究分析、售前对比和方案梳理；
- 项目推进、实施和交付协调任务。

不得仅凭问题类型、标题关键词或「不是产品需求」跳过门禁。功能与非功能需求共用同一目标：
判断需求本身是否足够向下流转。

### 2.3 输入

Skill 至少接收：

```text
issueKey
issueType
source_status
target_status
transition_id
source_assignee
summary
description
labels
reporter
assignee
created
updated
selected_fields
attachments[]:
  id / filename / mime_type / size / author / created / sha256
  / referencedByDescription / readable / extracted_text
comments[]:
  id / author / created / updated / body / referencedByDescription
  / explicitSupersedes / readable
external_links[]:
  url / referencedByDescription / readable / extracted_text / sha256
contentHash
gateVersion
dry_run
```

`readable` 必须区分：

- `text_extracted`：内容存在且文本可提取；
- `view_only`：内容存在但只能查看；
- `unreadable`：内容存在但无法解析或访问；
- `missing`：正文声称存在但实际不存在。

不得把无法读取、无法核实或未核验的材料写成已经验证。

### 2.4 内容哈希与门禁版本

接入层必须对参与判定的输入生成规范化哈希。推荐算法：

1. 只纳入标题、描述、参与判定的字段、附件元数据与内容哈希、外部链接内容哈希、可读性状态、非机器人业务评论。
2. 排除评论更新时间、页面抓取时间、内部日志、Jira 展示顺序等不稳定信息。
3. 数组按稳定键排序后序列化为规范化 JSON，再计算 SHA-256。
4. 任一参与判定的内容、可读性、版本或替代关系变化时，生成新的 `contentHash`。

`gateVersion` 至少由以下内容共同决定：

```text
skill_version
contractVersion
model_id_and_config
prompt_version
toolchain_version
```

幂等键为 `issueKey + contentHash + gateVersion`。执行异常不得作为最终结果缓存。

---

## 3. 材料来源与权威顺序

### 3.1 材料角色

Skill 必须为每份材料建立 `sourceRole`：

| 来源类型 | 含义 |
|---|---|
| `original_requirement` | 提交人对问题的原始描述、原始规则或原始样例 |
| `supporting_fact` | 用于补充现场、环境、样例、日志或现状的材料 |
| `technical_response` | 研发、算法、测试等对原始问题给出的方案、资源表、压测结果或技术答复 |
| `decision_record` | 产品、业务或技术负责人明确作出的范围、规则、目标或方案决定 |
| `historical_review` | 历史评审、旧结论、AI 预检报告或其他过程材料 |
| `other_reference` | 背景链接、通用文档、格式参考或无直接判定作用的材料 |

无法从元数据可靠判断时，输出 `sourceRole=unknown`，不得自行假定为原始需求。

### 3.2 证据优先级

判断原始诉求时按以下顺序：

1. 当前标题和描述；
2. 当前 Jira 结构化字段；
3. 提交人创建或明确引用的当前附件、外部链接；
4. 其他角色提供的补充事实；
5. 历史评论和讨论记录；
6. 历史评审、旧方案或其他参考材料。

高优先级材料与低优先级材料冲突时，不得由低优先级材料静默覆盖高优先级事实。
应记录冲突，并判断是否属于同层互斥。

### 3.3 原始需求与后续答复

- 同一 Jira 单后来出现的技术资源表、压测结果、方案文档和评审报告，不等于提交人最初的诉求。
- 后续技术答复与早期技术答复不同，通常属于方案演进；不得反过来要求原始提交人补充「为什么答案版本不同」。
- 只有原始描述明确要求这些后续材料作为本次诉求的核心输入时，才按核心证据处理。
- 附件作者、上传时间、评论作者和评论时间必须参与来源判断。
- 若测试复制或迁移丢失附件作者与时间，必须标记 `sourceRole=unknown`，不得直接用于否定原始需求。

### 3.4 历史评论

历史评论可以作为补充证据，但必须谨慎使用：

- 评论可以补充客户原话、背景、样例、链接和后续明确决策。
- 与当前描述或现行附件冲突时，不得直接采信评论。
- 若提交人或明确业务负责人使用「更正、更新、以此为准、替代前文」等表述，记录作者、时间、替代范围和影响，
  可将评论作为当前事实。
- 未声明替代关系的冲突，写入「建议澄清方向」或相应 finding，并注明评论时间和作者。
- 旧评论不得把缺失的核心事实补成「已确认」。

### 3.5 外部链接

- 正文写明「详细规则、数据或方案见该链接」时，该链接属于必需证据。
- 必需链接登录失败、无权限或内容无法提取时，返回 `executionStatus=execution_error`，不得生成内容通过/不通过。
- 仅作为背景或格式参考的链接不可读时，只生成非阻断建议。

---

## 4. 需求表达与观察重点

### 4.1 模板是参考，不是门槛

推荐参考团队《需求计划单填写模板》，但它只用于帮助表达，不是唯一合法格式。

Skill 按语义识别需求，不要求固定标题、章节名、编号或模板字段。以下均可接受：

- 「需求背景 / 业务背景 / 为什么提出」；
- 「问题说明 / 当前现状 / 当前现象与影响」；
- 「需求期望 / 期望结果 / 诉求说明 / 本期范围」；
- 「客户环境 / 客户环境与版本号 / 项目版本信息」；
- 编号、列表、表格或清楚分段的自然语言。

模板中的四段结构是帮助上游把话讲清楚：

| 参考要素 | 主要用于回答 |
|---|---|
| 需求背景 | 业务场景、当前能力、差异，以及为什么提出。 |
| 问题说明 | 当前实际发生什么、造成什么影响；有 BadCase 或证据时写清输入、当前结果和期望结果。 |
| 需求期望 | 使用场景、期望能力或规则、具体诉求、业务价值，以及希望下一步得到什么。 |
| 客户环境/版本号 | 客户、产品版本和部署环境；没有或不适用时写「无」或「不适用」。 |

上述要素可以合并表达。「需求期望」已经承接「目标、需求内容、期望结果」的语义，不要求拆成三个章节。
附件、截图、日志和外部链接属于证据，不单独计入四段结构。

### 4.2 结构状态

`structureStatus` 只能为：

- `structured`：有明确标题、编号、列表或独立段落，能清楚映射关键事实；
- `partially_structured`：没有标准标题，但分段或列表可以清楚识别关键事实；
- `unstructured`：只有一个概括句、口号、无分段整块文本，或无法识别关键事实。

`structured` 和 `partially_structured` 都可通过；不得因为缺少固定标题或四段不全而阻断。

### 4.3 Spec 覆盖

`specCoverage` 只用于说明模板语义覆盖情况，不直接决定结论：

- `present`：该语义有清楚内容；
- `partial`：只有部分信息，但仍可能足够继续处理；
- `missing`：没有对应内容；
- `not_applicable`：该要素明确不适用于本需求。

字段为：

```text
background
problem
expectation
environment
```

`environment` 缺失只有在会改变处理对象、方案或验证时，才可能成为阻断项。模板中写「无」或「不适用」视为已处理。

---

## 5. 需求类型与观察重点

### 5.1 需求类型

`requirementKind` 只能为：

| 值 | 适用事项 |
|---|---|
| `functional_change` | 功能新增、优化、规则调整、交互变化 |
| `defect_or_effect` | Bug、运行异常、AI/LLM 效果问题、质量不达标 |
| `technical_assessment` | 非功能要求、技术可行性、资源评估、性能容量评估 |
| `migration_or_load_test` | 迁移、适配、压测、兼容性支持 |
| `research_or_document` | 文档整理、研究分析、售前对比、方案梳理 |
| `project_task` | 项目推进、实施、交付协调等任务 |

问题类型不能直接等于需求类型。一个 `客户用户故事` 可能实际是 `technical_assessment` 或 `research_or_document`。

所有类型使用同一门禁目标：判断需求是否足以往下流转。类型只改变观察重点，不建立另一套结论，也不要求不同模板。

### 5.2 类型化观察重点

#### `functional_change`

需要能够理解：

- 使用或操作场景；
- 当前实际结果、问题或影响；
- 要改变的对象和范围；
- 期望结果或目标能力。

不要求提前写清产品最终规则、交互稿、技术实现或完整 AC。

#### `defect_or_effect`

需要能够理解：

- 真实业务任务或操作场景；
- 至少一个真实贴合业务、可追溯的代表性样例：输入或触发条件、当前实际输出、期望输出；
- 问题影响或当前差距；
- 新增能力无法提供 BadCase 时，提供目标场景样例和期望结果。

数量不限，不强制在门禁前量化指标、提供 Prompt、模型、评测集或完整评测协议。只有「效果不好」而没有场景和差距时，不通过。

#### `technical_assessment`

需要能够理解：

- 要评估的对象和场景；
- 规模、环境、版本和已知限制中与本次诉求直接相关的内容；
- 当前问题、约束或需要获得答复的原因；
- 希望得到的评估结果或交付物。

不得要求提交人先给出资源维度、编解码、模型路径、压测指标或技术方案；这些由接手方组织后续评估。

#### `migration_or_load_test`

需要能够理解：

- 迁移、适配或压测对象；
- 源对象和目标对象，或当前与期望验证范围；
- 当前数据、环境或版本条件；
- 期望达到的迁移或验证结果；
- 正文声称依赖的关键材料是否可读。

如果正文声称提供迁移清单、接口文档或测试数据，对应核心材料必须可读取，否则属于必需证据缺失或执行异常。

#### `research_or_document` / `project_task`

需要能够理解：

- 任务目标；
- 范围或对象；
- 期望产出；
- 必要的背景和限制。

不得要求此类任务提供产品功能规则、业务影响、技术方案或完整验收标准。

### 5.3 业务语义与领域常识

- 对团队已经有稳定业务含义的术语，按业务上下文解释。
- 例如语音 IVR 的「200 路并发」即为 200 路并发通话，不得机械追问是否是注册用户数。
- 只有存在会改变处理对象的合理竞争解释时，才归入 `material_ambiguity`。
- 不得用下游专业术语的完整度要求上游非专业提交人。

### 5.4 不阻断信息

以下信息缺失不单独阻断，只在确实有业务价值时给建议：

- 产品归属、截止日期、问题发现产品；
- 优先级、Story Points、批次编号、需求实现类型、内部工时；
- 产品方案、技术方案、详细测试方案、完整 AC；
- 性能压测环境、测量脚本、最终目标层级；
- 客户书面确认，除非正文把该确认本身定义成本次核心诉求；
- 最终由哪个角色接手。

---

## 6. 判定模型

判定不按字段是否齐全打分，而是判断接手方是否真正理解需求。必须依次完成以下三项检查。

### 6.1 检查一：事实是否清楚（`facts`）

需要能够识别：

- 需求对象和应用场景；
- 当前能力、当前结果或实际发生的问题；
- 问题影响、当前差距或提出需求的原因；
- 对技术评估、迁移、压测、研究和项目任务，类型化观察重点中的对象、环境和限制是否足以定位工作。

只有标题、口号、单句概括，或只有「详见附件」而正文没有核心事实，属于表达不足。

### 6.2 检查二：诉求与期望是否清楚（`request`）

需要能够识别：

- 提交方希望改变、解决、验证或产出什么；
- 外部要求、内部评估、当前差距和期望结果；
- 希望下一步获得的能力、结论、材料或评估答复。

允许存在多个候选目标、不同来源的差异或待决策问题，只要来源和差异明确。
方案未定、最终规则未定、技术实现未知，不等于诉求不清。

### 6.3 检查三：依据是否可信（`evidence`）

需要确认：

- 核心证据可读、可追溯；
- 材料作者、时间和来源角色足以判断权威性；
- 同一业务事实、同一权威层级、同一有效版本不存在互斥定义；
- 评论中的更正关系明确，未被误当作当前事实；
- 外部链接和附件的使用边界清楚。

补充材料不可读只给建议；正文依赖的核心材料不可读返回执行异常；正文声称存在但实际缺失或只有空模板属于不通过。

### 6.4 流转结论

只有在以下条件同时成立时，结论才为 `通过`：

1. 事实足以定位要处理的对象和问题；
2. 诉求与期望足以判断希望发生什么变化或得到什么结果；
3. 核心依据没有不必要的阅读或一致性障碍；
4. 仍待澄清的问题已经显式列出，或不存在。

「能否往下流转」是前三项检查的派生结果，不单独制造新的阻断项。

### 6.5 一致性判定

- 只有**同一业务事实、同一权威层级、同一有效版本**内的互斥定义构成冲突。
- 外部要求、内部评估、背景说明、技术答复可以并存，只要角色和来源清楚。
- 同一报表指标出现两套互斥公式，属于 `expectation_contradiction`。
- 早期技术资源表与后续修正表不同，标记版本关系，不默认属于原始需求冲突。
- 原始描述说「详见附件」，附件由提交人上传但只有空模板，属于 `required_evidence_missing`。
- 核心附件由提交人提供但无法解析，属于 `executionStatus=execution_error`；后续由研发提供的附件无法解析只影响该材料，
  不否定原始诉求。

### 6.6 决策与歧义

- 明确写出「客户要求 A、内部评估 B、待确认」可以流转，写入建议澄清方向。
- 明确写出「有两个候选方向，待选择」可以流转，不阻断。
- 同一事实没有说明可采用多种解释，且会影响处理对象或结果，属于 `material_ambiguity`。
- 产品方案、技术选型、实现方式不属于原始诉求歧义。

### 6.7 表达不足与事实缺口

- 只有标题、口号、一句概括，且无法定位对象和诉求，判 `unstructured_description`。
- 只有「详见附件 / 看截图 / 看日志」而没有核心诉求，判 `unstructured_description`。
- 分段或列表能清楚识别关键事实，可以通过。
- 表达结构可识别，但缺少某个继续处理必需的事实，按实际缺口分类，不重复报结构问题。
- 模板字段齐全但正文空洞，不通过，不得以字段形式代替内容。

---

## 7. 不通过问题分类

每条阻断问题只能归入一个主要分类；同一根因不得重复列出。

| 分类 | `category` | 适用情况 |
|---|---|---|
| 背景与需求内容不一致 | `background_content_mismatch` | 背景与当前诉求无关、方向相反或无法解释为什么需要该需求。 |
| 需求期望前后不一致 | `expectation_contradiction` | 同一事实、同一权威层级的目标、范围、公式或结果相互矛盾。 |
| 缺失关键信息 | `missing_key_information` | 缺少对象、场景、现状、诉求、期望等会阻止理解的事实。 |
| 关键表述有歧义 | `material_ambiguity` | 已有表述存在多种合理解释，且不同解释会改变处理对象或结果。 |
| 范围或归属不清 | `scope_or_ownership_unclear` | 无法判断需求对应客户、项目或系统边界。不得因未指定接手角色而阻断。 |
| 目标不可验证 | `unverifiable_target` | 只有「更快、更准、更稳定」等口号，且没有任何可识别结果。 |
| 关键事实未定 | `key_decision_unresolved` | 需求对象、当前问题或期望结果等核心事实仍写「待定」，导致无法理解；不包括方案待定。 |
| 需求结构不可识别 | `unstructured_description` | 只有标题、口号、无分段整块文本或正文无法识别关键事实。 |
| 必需证据缺失 | `required_evidence_missing` | 正文声称提供核心规则、数据、接口或材料，但实际缺失或只有空模板。 |
| 其他 | `other` | 无法归入以上分类时才使用，并说明原因。 |

### 7.1 去重规则

- 同一根因只出现一次。
- 同一分类下有多个独立子问题时，在一个 finding 下列出子问题，不重复生成相同分类。
- 「方案未定」「待决策」「技术实现未知」不得计入 `key_decision_unresolved`。
- 「未指定接手角色」不得作为 finding。
- 结构问题和具体事实缺口同时存在时，只选更根本的主要分类；通常优先 `unstructured_description`。

---

## 8. 判定流程

### 8.1 第一步：识别事项和需求类型

- 读取 `issueType`、来源、标题、描述和字段。
- 判断是否属于需求事项。
- 判断 `requirementKind`。
- 对所有需求类型使用同一门禁目标，并加载对应观察重点，不建立另一套门槛。

### 8.2 第二步：建立材料来源图

为每个附件、链接和评论记录：

```text
sourceId
sourceType
sourceRole
author
created
referencedByDescription
readable
currentVersion
contentHash
explicitSupersedes
```

未标明作者或时间时，标记 `unknown`，不得把 unknown 材料当作核心否定证据。

### 8.3 第三步：解析需求表达

- 标记需求背景、问题说明、需求期望、客户环境/版本号以及证据材料的原文位置。
- 输出 `structureStatus`、`structureEvidence` 和 `specCoverage`。
- 允许同义标题、合并表达和自然分段。

### 8.4 第四步：完成三项检查

- 检查事实是否清楚。
- 检查诉求与期望是否清楚。
- 检查依据是否可信。
- 对条件场景检查对应类型化观察重点。
- 对核心材料检查可读性与同层一致性。

### 8.5 第五步：生成结论

- 三项检查无阻断问题：`pass`。
- 任一检查存在阻断问题：`fail`，列出全部独立阻断项。
- `advisoryFindings` 和 `fieldHints` 不参与结论。
- 通过时生成需求评估摘要，不判断或输出建议接手角色。

---

## 9. 输出契约

### 9.1 完整示例

`result.json` 只使用驼峰字段，脚本不接受其它写法。

```json
{
  "schemaVersion": "1.2",
  "issueKey": "KEY-123",
  "issueType": "客户用户故事",
  "conclusion": "pass",
  "applicability": "requirement",
  "requirementKind": "functional_change",
  "structureStatus": "structured",
  "structureEvidence": [
    "描述以「业务背景」开头，包含客户现状与问题",
    "以编号列出当前现象、影响和期望能力",
    "以「客户环境/版本号」说明客户、版本和环境"
  ],
  "specCoverage": {
    "background": "present",
    "problem": "present",
    "expectation": "present",
    "environment": "present"
  },
  "materialSourceMap": [
    {
      "sourceId": "attachment:10001",
      "sourceType": "attachment",
      "sourceRole": "original_requirement",
      "author": "reporter-user-key",
      "created": "2026-11-25T10:00:00+08:00",
      "referencedByDescription": true,
      "readable": "text_extracted",
      "currentVersion": true,
      "contentHash": "sha256:...",
      "explicitSupersedes": null
    }
  ],
  "checks": [
    {
      "id": "facts",
      "name": "事实是否清楚",
      "status": "pass",
      "summary": "描述写清客户、语音 IVR 场景、200 路并发和当前部署问题",
      "evidenceRefs": ["description:paragraph-1"]
    },
    {
      "id": "request",
      "name": "诉求与期望是否清楚",
      "status": "pass",
      "summary": "明确请求研发评估所需资源配置，并说明期望交付物",
      "evidenceRefs": ["description:paragraph-2"]
    },
    {
      "id": "evidence",
      "name": "依据是否可信",
      "status": "pass",
      "summary": "正文足以理解诉求，当前无核心证据冲突",
      "evidenceRefs": ["description:paragraph-1"]
    }
  ],
  "findings": [],
  "advisoryFindings": [],
  "fieldHints": [],
  "assessmentSummary": {
    "background": "…",
    "goals": ["…", "…"],
    "points": ["…", "…"],
    "clarifications": [
      {
        "question": "…",
        "reason": "…",
        "impact": "scope"
      }
    ],
    "evidenceRefs": ["description:paragraph-1", "attachment:10001"]
  },
  "executionError": null,
  "executionStatus": "success",
  "executionStage": "completed",
  "gateRunId": "g1pre-20261125T120000-abc123",
  "previousGateRunId": null,
  "ruleVersion": "V1.2",
  "contractVersion": "1.0",
  "gateVersion": "V1.2+model-v1+prompt-v1+tool-v2.3.1",
  "contentHash": "sha256:...",
  "commentId": null,
  "pinStatus": "pending",
  "jiraComment": "h3. (/) G1-Pre 通过\n=====需求评估摘要（by G1-PRE agent）=====\nh4. 1. 需求背景\n…"
}
```

### 9.2 字段规则

- `schemaVersion`、`ruleVersion`、`contractVersion`、`gateVersion`、`contentHash` 必须存在。
- `applicability` 只能是 `requirement` 或 `not_applicable`。
- `applicability=requirement` 时，`conclusion` 只能是 `pass` 或 `fail`，`requirementKind` 必须存在。
- `applicability=not_applicable` 时，`conclusion=null`、`requirementKind=null`、`executionStatus=not_applicable`，
  只返回分流理由，不写门禁评论。
- `findings` 非空时必须为 `fail`；为空时必须为 `pass`。
- `assessmentSummary` 只在 `pass` 时生成；`fail` 时必须为 `null`。
- `checks` 必须覆盖事实、诉求与期望、依据三项检查；条件场景可以追加检查项，但不得改变三项主检查。
- `evidenceRefs` 必须引用标题、描述、字段、附件、链接或评论的可定位事实。
- `advisoryFindings` 和 `fieldHints` 不参与结论，也不写入评论。
- 通过评论不显示规则版本、内容哈希、gateVersion 或内部检查表。

### 9.3 Finding 结构

每个 `findings[]` 项必须包含：

```json
{
  "id": "F1",
  "category": "missing_key_information",
  "checkId": "facts",
  "title": "缺少当前问题与影响",
  "currentProblem": "…",
  "evidenceRefs": ["description:paragraph-1"],
  "minimalAction": "…",
  "impact": "scope"
}
```

字段规则：

- `checkId` 只能是 `facts`、`request`、`evidence`。
- `impact` 只能是 `scope`、`solution`、`verification`、`routing`、`unknown`。
- `currentProblem` 必须描述当前缺口，不得替提交人编写完整需求。
- `minimalAction` 只说明最少补充什么，不得要求产品方案、技术方案或完整验收标准。
- 同一根因只生成一个 finding。

### 9.4 执行异常结构

执行异常时：

```json
{
  "conclusion": null,
  "executionStatus": "execution_error",
  "executionStage": "attachment_parse",
  "executionError": {
    "stage": "attachment_parse",
    "errorCode": "unreadable_required_attachment",
    "sourceId": "attachment:10001",
    "retryable": true,
    "message": "核心附件无法解析，无法确认正文依赖的规则"
  },
  "assessmentSummary": null
}
```

执行异常不得写 `G1-Pre 通过` 或 `G1-Pre 不通过` 内容评论。

---

## 10. Jira 评论

评论用于让提交人和下一接手方快速理解结论、需要补充什么，以及接手后应该看什么。
固定使用 Jira Wiki Markup，不输出 Markdown。

### 10.1 通过

固定标题必须使用 Jira 图标：

```text
h3. (/) G1-Pre 通过
=====需求评估摘要（by G1-PRE agent）=====

h4. 1. 需求背景
…

h4. 2. 需求目标
…

h4. 3. 需求要点
* …
* …

h4. 4. 建议澄清方向
* 需要确认：…
  原因：…
* 需要确认：…
  原因：…

_由 G1-Pre 基于本次原始材料生成，用于需求接手前快速理解，不构成产品、技术或验收方案。_
```

规则：

- 标题行和「需求评估摘要」分隔行固定保留。
- 建议澄清方向可以为空，此时写「无」。
- 需求评估摘要只能总结原始材料，不得新增规则、字段、接口、方案或验收标准。
- 「需求目标」是原始诉求中的目标，允许多个候选目标、不同来源和待决项，不得升级为最终产品目标。
- 评论末尾固定保留免责声明。
- 不输出建议接手角色。
- 评论写入成功后由接入层置顶；置顶失败不改变通过结论，但返回 `executionWarning` 并重试。

### 10.2 不通过

固定标题：

```text
h3. (!) G1-Pre 不通过（2项）
*报告人：* [~reporter-user-key]
*本次阻断项：*
# 同一报表指标存在两套互斥公式
# 核心迁移附件缺失

h4. 1. 需求期望前后不一致
*当前问题：* …
*依据：* …
*最小补充动作：* …

h4. 2. 必需证据缺失
*当前问题：* …
*依据：* …
*最小补充动作：* …
```

规则：

- 先列 N 个阻断项摘要，再逐项解释。
- `N` 只统计阻断项，不包含建议。
- 报告人 mention 使用 Jira 用户 key。
- 每条 finding 固定使用「当前问题、依据、最小补充动作」。
- 一次列出全部独立阻断项。
- 不通过时不生成需求评估摘要。
- 不显示规则版本、内容哈希、gateVersion 或「G1-Pre 指纹」。

### 10.3 执行异常

执行异常不生成 `G1-Pre 通过` 或 `G1-Pre 不通过` 内容评论，而由接入层记录并重试：

```text
G1-Pre 执行异常：核心附件无法解析
errorStage: attachment_parse
sourceId: attachment:10001
```

---

## 11. Jira 接入行为

### 11.1 通过

1. 生成 `gateRunId` 和需求评估摘要。
2. 写入通过评论。
3. 尝试置顶最新通过评论。
4. 记录 `commentId`、`pinStatus` 和 `executionStatus=success`。
5. 允许原 transition 继续。

### 11.2 不通过

1. 生成新的 `gateRunId` 和不通过评论。
2. 接入层取消本次 transition。
3. 保持源状态；若工作流要求「打回待办」，执行明确配置的回退 transition。
4. 回退到发起门禁前的 `sourceAssignee`；无法取得历史经办人时回退报告人，并记录 `executionWarning`。
5. 不生成需求评估摘要。
6. 不自动修改优先级、Story Points、产品归属、截止日期等其他字段。

### 11.3 执行异常

1. 不生成内容通过/不通过结论。
2. 保持源状态和经办人。
3. 记录 `executionStatus=execution_error`、`executionStage`、`sourceId` 和异常摘要。
4. 允许重试；禁止静默放行。

### 11.4 幂等与复检

- 同一 `issueKey + contentHash + gateVersion` 已处理过时，复用已有 `gateRunId`、结论和需求评估摘要，不重复评论。
- 标题、描述、当前附件哈希、链接内容、参与判定的字段、可读性状态或影响判定的更正评论变化后，
  生成新的 `contentHash`。
- 每次内容或规则版本变化后重新执行，形成新的独立 `gateRunId` 和新结论。
- 历史评论和结论不可修改、覆盖或删除。
- 最新一次通过评论置顶；旧评论原样保留，供追溯当时材料、版本和判定。
- `contentHash` 不包含内部机器人评论、展示顺序和无业务含义的时间戳变化。
- **本仓库落地**：评论中不保存指纹，因此 `g1-pre.mjs history <KEY|URL> --file <result.json>` 会把本轮
  渲染结果与最新一条 G1-Pre 评论正文比对；正文一致时不重复评论。`contentHash` 仍写入 `result.json`
  供审计，但不用作评论判重的唯一依据。

---

## 12. 禁止事项

- 不要把 G1-Pre 当成只针对产品功能需求的门禁。
- 不要因为需求是非功能、效果、技术评估或项目任务就绕过门禁。
- 不要判断或输出必须由哪个角色接手。
- 不要把「方案未定」当成需求不合格。
- 不要因为需要后续决策而阻断向下流转。
- 不要要求提交人回答编解码、模型路径、资源维度、接口字段等下游技术答案。
- 不要把后续研发、测试或其他角色的补充材料当成原始需求，再反向要求提交人解释版本差异。
- 不要因为材料作者或时间未知，就用其内容否定原始诉求。
- 不要因为未使用模板标题或四段不全而阻断。
- 不要把模板字段齐全但正文空洞的需求判为通过。
- 不要凭标题通过，也不要因描述短而直接拒绝。
- 不要把技术术语缺失、专业细节不足或精确验收协议缺失一律当成阻断。
- 不要让历史评论覆盖当前描述和现行附件，除非存在明确的更正与替代关系。
- 不要杜撰证据或声称读取了不可读附件。
- 不要重复列出同一根因。
- 不要在 Jira 评论中展示内部推理、完整检查表、规则版本或内容指纹。
- 不要直接修改 Jira 状态或绕过工作流；由接入层执行。

---

## 13. 接入验收

- 五种目标 transition 分别验证通过、不通过和执行异常。
- 功能、效果、非功能、技术评估、迁移压测、研究文档和项目任务均使用同一门禁目标。
- 通过：评论写入成功并置顶，`pinStatus` 和 `commentId` 可追溯；置顶失败返回 `execution_warning`。
- 不通过：原 transition 被取消，源状态保持或按工作流打回待办，并回退到发起前经办人。
- 执行异常：不生成通过/不通过内容结论，保持源状态和经办人，可重试且不得静默放行。
- Jira 评论标题分别显示 `(/)` 或 `(!)` 图标；括号中的 N 只统计阻断项。
- 通过评论固定出现 `=====需求评估摘要（by G1-PRE agent）=====`，不输出建议接手角色。
- Jira 评论不显示 ruleVersion、contentHash、gateVersion 或内容指纹。
- 同一内容哈希和门禁版本不重复评论；内容或规则变化后生成新的独立结论。
- `dry_run=true` 不写评论、不置顶、不修改字段、不执行 transition。
- 核心附件不可读时返回执行异常；补充附件不可读只给建议。
- 结构化输出必须符合第 9 节字段规则；finding、check、summary 和执行异常均要可校验。

---

## 14. 本仓库验收场景

| 场景 | 期望结果 |
|---|---|
| 对象、场景、现状、问题和期望均完整，且描述分层写清但篇幅较短 | `通过`，评论生成需求评估摘要 |
| 仅缺产品归属、截止日期或问题发现产品 | `通过`，只写入建议澄清方向 |
| 只有标题、一句话或一句口号式描述 | `不通过`，`unstructured_description` |
| 只写「详见附件」而正文没有核心诉求 | `不通过`，`unstructured_description` |
| 正文提到截图或日志但材料缺失 | `不通过`，`required_evidence_missing` |
| 正文与附件对目标或范围表述相反 | `不通过`，`expectation_contradiction` |
| 背景与需求目标无关 | `不通过`，`background_content_mismatch` |
| 已有表述存在两种合理解释且会导致不同结果 | `不通过`，`material_ambiguity` |
| 性能类缺少业务影响、基线或目标 | `不通过`，`unverifiable_target` |
| 多个诉求均表达完整 | `通过` |
| 多个诉求中仅某一项缺少关键事实 | `不通过`，只列该项缺口 |
| 正文空洞但模板字段名称齐全 | `不通过` |
| 核心附件不可读 | `executionStatus=execution_error`，不写通过/不通过评论 |
| 补充附件不可读 | 只给建议，不改变结论 |
| 同一内容重复运行、最新评论正文一致 | 不新增评论 |
| `dry_run=true` | Jira 上没有任何新增评论 |
