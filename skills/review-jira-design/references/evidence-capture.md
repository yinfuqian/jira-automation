# 可视化证据截取

本流程用于把原型问题变成可定位的内联证据。证据挂在对应问题下方，默认折叠。它是 G1 报告的增强，不改变 PRD、原型和结论规则。

## 截取前

1. 锁定用户指定的具体 HTML、版本、页面状态和视口；不要从相似文件中自行挑选。
2. 为问题确定一个稳定定位方式：CSS selector、页面入口 + 操作路径，或源码行号。优先 selector 和可复现路径，行号只作补充。
3. 从 PRD 取最小必要摘录，记录章节或 `G / FR / BR / IR / AC`，不要把整篇 PRD 放进报告。

## 截取原型

有可用浏览器时，使用本目录的 `scripts/capture_prototype_evidence.mjs`：

```text
node scripts/capture_prototype_evidence.mjs \
  --input "<HTML绝对路径或URL>" \
  --selector "<问题区域selector>" \
  --out "<截图路径>" \
  --viewport 1440x900
```

脚本会打开指定文件、等待页面资源渲染约 8 秒、在目标区域周围保留上下文并输出 PNG。运行顺序是：Playwright 管理浏览器 → 本机 Chrome/Edge 的 Playwright executable → Chrome/Edge headless CLI。CLI 采用指南约定的 `1440 × 900` 首屏、PNG、`file://` 优先；`file://` 失败且输入是本地 HTML 时，自动启动临时 `python -m http.server` 重试。

当使用本机浏览器或 CLI 时，输出 JSON 会写明 `captureMode` 和 `selectorApplied`。`selector-system-browser` 仍可按 selector 局部截取；`viewport` 或 `viewport-http-fallback` 只代表首屏视口截图，selector 没有应用，报告中必须标注为“首屏证据”，同时保留 HTML 路径和 selector 作为定位信息。

截图完成后脚本会校验 PNG 签名、文件大小和 `1440 × 900` 尺寸，并用 `--dump-dom` 检查 `ErrorResponse`、`ERR_FILE_NOT_FOUND`、`ERR_CONNECTION_REFUSED` 等错误标记。selector 不存在、页面错误或资源导致状态不可复现时，命令失败或输出未验证原因；不要用整页截图冒充目标区域证据。

如果问题只在操作后出现，先按最小路径操作到目标状态，再截取；在报告中记录入口、操作、数据条件和最终状态。不要提交真实业务数据或执行真实写操作。

## 内联证据格式

每张证据包含：

```text
证据 ID：E-001
问题 ID：A-001
PRD 摘录：章节/稳定 ID + 最小原文
原型截图：PNG data URL + 页面/selector + 视口 + 状态
实际观察：截图中能确认的事实
定位与证据类型：selector / 行号 / 操作路径
```

差异影响和建议动作写在所属问题里，证据区不重复。

在报告中，证据内联在对应问题下方并默认折叠：

- 折叠行只写「PRD 要求 与 原型实际」，**不再挂 `E-00x` 徽标**（证据编号已并入问题编号体系，避免重复信息）。
- 展开区固定为「左 PRD 要求 ‖ 右 原型实际」两栏，左栏下方给 PRD 出处、右栏下方给原型出处。
- 代码定位（selector、行号、函数名）放在展开区最后一行「技术定位」，灰色等宽小字；正文里不允许出现。
- 截图放在对照之后；无截图时整段移除，并在展开区末尾写明「未验证」及原因。

证据内容不需要手写 HTML，按 `references/content-example.json` 的字段填进内容 JSON 的 `问题[]` 即可：

| 字段 | 内容 |
| --- | --- |
| `PRD要求` | 最小必要原文摘录（保留编号，脚本会自动换成可读文案 + 浮层） |
| `原型实际` | 截图中/源码中可确认的事实，写成可读结论，不写代码 |
| `出处` | 缺省取 `需求依据`，形如 `PRD §3.3.4 BR-002、§5.2 状态—权限—操作矩阵、§4.3 AC-003` |
| `原型出处` | 如 `原型 第 985-989 行（五按钮）、第 1310-1311 行（禁用条件）` |
| `技术定位` | 如 `.batch-actions / agentExportHandleBatch · 行 985-989 … · :disabled="agentExportBatchDisabled"` |
| `截图` | `[{ "说明": "截图说明", "data": "data:image/png;base64,..." }]`，无截图给空数组 |
| `核对说明` | 数组，例如 `["未验证：原型依赖内网资源，评审环境不可达，页面未在超时内渲染"]` |

版式由模板与生成脚本统一控制，不要手写：白底 + 细线分区；对照区两栏之间只留一条竖向分隔线；截图单张限宽 720px、最高 460px，多张自动并排。

## 无法截取时

- HTML 无法打开、内网资源不可用、selector 不稳定或目标状态无法复现：标记 `截图未验证`。
- 改用精确的 HTML 路径、selector/行号、源码片段或操作路径，并说明缺少哪一步运行证据。
- 不根据页面结构猜测渲染结果，也不把截图缺失自动判为 PRD 问题；结论按现有证据状态判定。


