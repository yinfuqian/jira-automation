# 一键执行口令：阶段三｜复核后方案与测试点 → 最终交付

请按 `prompts/contract.md` 执行。阶段三只转换人工审核后的最终边界，不重新设计用例。

## 标准口令

```text
/qa-cases <Jira需求Key或URL>
```

## 输入

- 主需求材料：从 Jira 单当前最新需求附件自动读取/下载（必需）；不使用 `inputs/` 下旧副本。
- 人工复核后的测试方案：按 Jira Key 与 scope 从 `outputs/<scope>/<JiraKey>/test-plans/` 读取最新版本（必需），是范围、策略、资源、风险和取舍基线。
- 人工审核后的测试点 XMind：按 Jira Key 与 scope 从 `outputs/<scope>/<JiraKey>/test-cases/` 读取最新版本（必需），是唯一最终测试点基线。
- Jira 参数：必需；本次显式 Key/URL 必须与交付物文件名前缀一致；多版本无法唯一确定时停止。

## 阶段门禁与目录

测试方案和 XMind 必须位于同一：

```text
outputs/<scope>/<JiraKey>/
```

Jira Key、需求名称或 scope 冲突时停止，不跨目录寻找替代文件。

标准输出：

```text
outputs/<scope>/<JiraKey>/test-plans/<JiraKey>-<需求名称>-测试方案-v<版本>.md
outputs/<scope>/<JiraKey>/test-cases/<JiraKey>-<需求名称>-测试用例.xlsx
outputs/<scope>/<JiraKey>/test-cases/<JiraKey>-<需求名称>-Jira导入.csv
```

## 执行步骤

1. 读取 Jira 最新需求附件，并按 Jira Key 与 scope 定位最新人工复核测试方案和审核 XMind；校验三者 Jira Key、需求名称、scope 和需求交付根目录一致。
2. 识别正式叶子：标题合规、无业务子 topic、notes 完整且不带 `【待确认】`。
3. 对照最终 XMind 刷新测试方案的范围、重点、策略、风险、资源和测试数据要求。
4. 按 XMind 人工审核顺序一叶一用例转换，不跨叶合并、不拆分叶子、不按优先级或类型重新排序。
5. 从 XMind notes 原样继承页面路径、用例边界、步骤与预期关系、优先级、中文主要类型和可选“测试数据”；测试数据存在时写入 Excel“测试数据”和 CSV `Data`，不存在时两处均为空，不得推测或补写。
6. 生成 Excel；“用例类型”只能填写一个标准中文枚举，并生成类型数量和占比统计，十种类型均保留统计项，未使用类型显示 0。
7. 生成 Xray CSV；保持固定字段，不新增 `type-*` 或其他未经确认的类型映射。
8. 输出自动化适配建议。
9. 回读最终方案、Excel 和 CSV，执行机械一致性和导入完整性校验。

## 阶段三禁止静默修改

不得静默改变：

- 业务语义和场景目标；
- 页面或技术入口；
- 一叶一用例边界；
- 步骤与预期关系；
- 风险定级和主要类型。

只允许去除首尾空白、统一换行、CSV 转义、固定优先级映射、连续编号、安全路径拼接，以及明确不改变业务含义的错别字、空格和标点修正。可能改变业务含义的文字问题必须退回人工审核；发现标题重复、不可执行、类型冲突或覆盖问题时退回阶段二，不在阶段三直接删除、合并、拆分或重新分类。

## Excel 与 Xray 规则

- Excel 字段：用例编号、模块/分类、用例标题、前置条件、测试数据、测试步骤、预期结果、优先级、用例类型、是否适合自动化、Requirements。
- Excel 优先级使用 P0/P1/P2/P3；CSV Priority 映射 Highest/High/Medium/Low。
- 阶段三从审核后的 XMind 原样继承用例等级，不重新定级：P0 为基础核心且每迭代回归、自动化优先；P1 为重要高频功能且每迭代回归；P2 为一般、低频、边界、异常或兼容场景且全量回归执行；P3 为生僻低价值场景且全量回归时评估执行。
- XMind 任务优先级必须与 notes 用例等级一致：P0→`priority-1`、P1→`priority-2`、P2→`priority-3`、P3→`priority-4`。
- Excel 用例类型使用单个中文枚举；CSV 不承载主要类型。
- CSV 字段采用 Xray 导入标准版本 3，固定为：Summary、Description、Test Repository Path、Priority、Labels、Test Steps、Data、Expected Result、External ID、Requirements。
- Excel“模块/分类”和 CSV `Test Repository Path` 使用相同的 `需求名称 / XMind业务层级 / 测试分类` 可变层级路径；第一级固定为需求名称，中间层继承审核后 XMind 业务语义 topic，末级为测试分类。过滤 `业务管理`、`业务配置`、`业务管理 > 业务配置` 等通用菜单包装层级，统一使用 `/` 分隔且不保留 `>`。
- Excel“测试数据”对应 CSV `Data`，CSV `Data` 映射 Xray `Data`；前置条件继续写入 `Description`，测试数据不在 `Description` 中重复。
- `Requirements` 每行填写目标 Jira Key，并映射为 `Link "Tests"`；不得使用 `relates to`。

## 100% 强制校验

- [ ] XMind 正式叶子数 = Excel 数据行数 = CSV 数据行数。
- [ ] 一叶一用例，标题映射唯一。
- [ ] 文件名前缀、Excel 用例编号、CSV External ID 和 Requirements 使用同一完整 Jira Key。
- [ ] ID 从 TC001 连续递增、非空且唯一；Excel 与 CSV ID 集合一致。
- [ ] 所有前置条件从 1 连续编号；步骤与预期编号、数量和编号集合一致。
- [ ] 标题唯一；发现重复标题已退回阶段二人工修改。
- [ ] notes 中不在中文行为描述里添加无意义空格，技术内容保留必要空格，各固定段落结尾保留换行。
- [ ] XMind 每个测试点叶子的任务优先级与 notes 中 P0/P1/P2/P3 一致。
- [ ] 人工确认路径在 XMind notes、Excel 和 CSV 中一致。
- [ ] XMind notes、Excel“测试数据”和 CSV `Data` 的可选测试数据内容及空值状态一致。
- [ ] XMind 和 Excel 的主要类型一致；CSV 不参与类型一致性校验。
- [ ] Excel 每条用例只有一个中文类型，类型统计合计等于用例总数；未使用类型显示 0。
- [ ] XMind ZIP、`content.xml` 和根 topic 结构可解析；XMind 2.0 核心元素使用默认命名空间，不存在 `ns0:` 等重写前缀。
- [ ] CSV 为 UTF-8 BOM、英文逗号分隔且固定10列表头完整。
- [ ] Requirements 全部非空且等于目标 Jira Key。

任何机械一致性或导入完整性校验失败均不得正式交付；业务覆盖和内容合理性以人工审核结论为准。
