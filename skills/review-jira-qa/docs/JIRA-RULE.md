# Jira 规则对接说明（平台侧）

本包**不实现 HTTP 服务**。Jira 自动化规则的「发送网络请求」由技能平台接收，平台安装并调用 `review-jira-qa` 技能。

## 推荐触发点

优先在需求流转到 **「评审中」** 时触发阶段一：

- 产品把需求推进到评审中 → 平台执行技能 `review-jira-qa`（输入 `risk-analysis <KEY>`）→ 产出准入门禁清单并回写 Jira。
- 结论「通过」：允许评审继续。
- 结论「暂不通过」：产品按清单修订材料后复跑；规则可停留在评审中。

也可以把触发点放在 **「开发设计中」** 作为最后门禁：

- 流转到开发设计中 → 平台执行阶段一。
- 结论「暂不通过」：规则把单子退回「评审中」（与示例规则「门禁：需求推进到开发设计中未通过 → 退回评审中」一致）。
- 结论「通过」：允许进入开发设计。

> 触发状态由 Jira 规则配置；技能本身不检查状态，只按传入的 Issue Key 执行。

## 平台调用契约

- 技能名：`review-jira-qa`
- 输入：`risk-analysis <ISSUE-KEY>`；只传 `<ISSUE-KEY>` 时默认阶段一。
- 环境变量：
  - `JIRA_BASE_URL`
  - `JIRA_PAT`
  - `QA_WORKSPACE`（建议每次运行使用独立空目录）
  - 可选 `QA_SCOPE`、`QA_TZ_OFFSET`
- 运行目录：`QA_WORKSPACE`；产物写 `inputs/`、`outputs/`、`.qa/`。
- 不需要平台传入材料路径；技能每次从 Jira 当前最新附件刷新。

## 结果判定

技能结束时 stdout 末尾输出：

```text
QA_RESULT_JSON
{"schemaVersion":1,"stage":"risk-analysis","ok":true,"jiraKey":"KEY-1","conclusion":"通过|暂不通过","failedItemCount":0,"deliverableVersion":"1.0",...}
```

- `ok=true` + `conclusion=通过`：规则允许继续流转。
- `ok=true` + `conclusion=暂不通过`：规则退回「评审中」。
- `ok=true` + `status=SKIPPED` + `applicable=false`：输入不适用门禁（无附件且无 Jira 需求描述、主材料不唯一），**继续流程、不告警、不中断**；Jira 输出一条「评审不适用」评论，不写门禁结论。
- 只有 PRD、没有 HTML：正常评审；没有 PRD/HTML 附件但有 Jira 需求描述：以 Jira 字段为主需求材料正常评审，不中断。
- `ok=false` + `status=ERROR`：执行异常，**exit 0、不阻塞**；阶段一尽量写一条「执行异常」评论，阶段二/三只写本地 result/日志，不写 Jira 评论。

阶段一是主任务，完成即代表本次调用完成；技能不改变需求状态、不改门禁结论。阶段二/三是独立子任务，不修改阶段一产出与评论。

平台若需要同步返回给 Jira Automation，读取上述 JSON；包内不提供 HTTP 响应。

## 同材料复用与材料更新

- **同材料复用**：当前材料附件 ID 集合与上一版门禁评论一致时，`reuse.mjs` 返回 `REUSE=true`，直接复用上一版结论；不重新分析、不新增附件/评论，保证同版本材料结果稳定。
- **材料更新后**：按 `coverage-required.json` 覆盖全部 AC，并做同类 AC 扫描；本版新增不通过项标注「本版材料变更引入」或「上一版漏检」，不得把上一版漏检写成新材料问题。
- 需要强制重审时使用 `reuse.mjs --force`。

## 并发与幂等

- 同一 Issue Key 的写操作使用 `.qa/locks/<KEY>.lock` 串行化。
- 同一材料版本重复执行：附件同名同大小跳过；评论按「标题 + 材料版本」定位后编辑，不重复新建。
- 材料附件变化后复跑才生成新版本附件与新评论。

## 失败处理

| 情况 | 退出码 | status / errorCode | 行为 |
|---|---|---|---|
| 缺 `JIRA_BASE_URL`/`JIRA_PAT` | 0 | ERROR / USAGE | 无法写评论；本地 result 留痕 |
| PAT 无效 | 0 | ERROR / JIRA_AUTH | 无法写评论；本地 result 留痕 |
| 单不存在 | 0 | ERROR / JIRA_NOT_FOUND | 无法写评论；本地 result 留痕 |
| 无需求附件 / 无需求侧材料 / 主材料不唯一 | 0 | SKIPPED | 写「评审不适用」评论，平台继续流程 |
| HTML/XMind/方案/用例校验失败 | 0 | ERROR / VALIDATION | 阶段一写「执行异常」评论；阶段二/三只本地留痕 |
| 上传/删除/评论失败 | 0 | ERROR / JIRA_WRITE | 本地文件保留，旧版本不删除；阶段一尽量写「执行异常」 |

所有情况均以 exit 0 结束，平台据 `status` / `applicable` 判断是否继续，不因非零退出中断流程。

阶段二/三不由平台自动调用；必须人工触发并满足人工门禁，且不写 Jira 评论、不影响阶段一结论。
