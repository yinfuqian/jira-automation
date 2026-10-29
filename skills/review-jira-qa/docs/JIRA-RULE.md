# Jira 规则对接说明（阶段一）

本技能在仓库中的注册名是 `review-jira-qa`，**只执行阶段一需求准入门禁**。
本包不实现 HTTP 服务；Jira 自动化规则由技能平台接收，平台按 `skill_id=review-jira-qa`
启动 Codex Agent 并传入 Jira 链接。

## 推荐触发点

- 需求流转到「评审中」时触发阶段一；
- 结论「通过」：允许评审继续，不流转状态；
- 结论「暂不通过」：由父技能或 Jira 规则决定回退；`jira-gate-1` 调用时其最终结论固定为
  「退回补充」，由 G1 自行流转到「需求设计」。

技能本身不检查 Jira 状态，也不修改状态。

## 平台调用契约

- 技能名：`review-jira-qa`
- 输入：Jira Issue Key 或 `/browse/<KEY>` URL；平台注入 `JIRA_BASE_URL`、`JIRA_PAT`
  （兼容 `JIRA_TOKEN`）与 `QA_WORKSPACE`。
- 运行目录：`QA_WORKSPACE`；产物写 `inputs/`、`outputs/`、`.qa/`。
- 每次从 Jira 当前最新附件刷新材料，不需要平台传入材料路径。

## 执行流程

```text
prepare.mjs --key <KEY> --stage 1
  ├─ preflight
  ├─ jira-read --comments 10
  ├─ jira-download
  ├─ reuse
  ├─ materials
  └─ coverage
        ↓
READY / REUSED / SKIPPED / ERROR
        ↓ READY
核读 materials-digest.md
        ↓
scan.mjs → candidates.json
        ↓
写 gate.spec.json（checks / coverage / scans / failures / materials）
        ↓
render-gate.mjs → HTML + Jira wiki 评论
        ↓
validate-gate.mjs --coverage-required --candidates --source
        ↓
jira-upload.mjs → jira-comment.mjs → result.mjs
```

`REUSED` 直接复用上一版结论，零写入；`SKIPPED` 写「评审不适用」评论后停止；
`ERROR` 记「未完成」并交还调用方，不阻塞父流程。

## 结果判定

技能结束时 stdout 末尾输出：

```text
QA_RESULT_JSON
{"schemaVersion":1,"stage":"risk-analysis","ok":true,"jiraKey":"KEY-1","conclusion":"通过|暂不通过","failedItemCount":0,"deliverableVersion":"1.0",...}
```

- `ok=true` + `conclusion=通过`：阶段一准出通过。
- `ok=true` + `conclusion=暂不通过`：业务完成但存在阻断项；G1 最终结论为「退回补充」。
- `status=SKIPPED` + `applicable=false`：输入不适用本阶段，**继续流程、不告警、不中断**；
  Jira 侧会留一条「评审不适用」说明评论，不写门禁结论。
- `status=ERROR` / `ok=false`：执行异常，**exit 0、不阻塞**；阶段一尽量写一条
  「执行异常」评论，父流程记为「未完成」。

## 同材料复用

- `prepare.mjs` 内部调用 `reuse.mjs`；当前材料附件 ID 集合与上一版门禁一致时返回
  `status=REUSED`，直接复用上一版结论，不重新分析、不新增附件或评论。
- 需要强制重审时加 `--force`。
- 材料更新后重新做全量 AC 覆盖与结构扫描，本版新增不通过项标注
  「本版材料变更引入」或「上一版漏检」，不得把上一版漏检写成新材料问题。

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
| HTML/门禁校验失败 | 0 | ERROR / VALIDATION | 写「执行异常」评论，不产生半截结论 |
| 上传/删除/评论失败 | 0 | ERROR / JIRA_WRITE | 本地文件保留，旧版本不删除 |

所有情况均以 exit 0 结束，平台据 `status` / `applicable` / `ok` 判断是否继续，
不因非零退出中断流程。阶段二/三不对外启用。
