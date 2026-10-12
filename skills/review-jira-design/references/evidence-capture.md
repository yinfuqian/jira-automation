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

在报告中，证据不单独成章，而是内联在对应问题下方并默认折叠。问题标题行放 `.evidence-toggle` 按钮（`data-evidence="evidence-A-00x"`），展开区的容器为 `.evidence-inline`（`id="evidence-A-00x"`，默认带 `hidden`）。展开区标题行显示证据编号、`对应 A-00x`、证据标题和“收起”按钮。没有证据的问题不渲染按钮和展开区。按以下对应关系填入 `references/report-template.html` 的 `{{问题卡片}}`：

```html
<div class="evidence-inline" id="evidence-A-001" hidden>
  <div class="evidence-inline-head"><span class="evidence-id">E-001</span><span class="evidence-ref">对应 A-001</span><h4>证据标题</h4><button class="evidence-toggle" type="button" data-evidence="evidence-A-001" data-fixed="收起" aria-expanded="true">收起</button></div>
  <div class="compare">
    <div class="side prd"><span class="k">PRD 要求</span><p>最小必要原文摘录</p></div>
    <div class="side proto"><span class="k">原型实际</span><p>截图中可确认的事实</p></div>
  </div>
  <div class="shots"><figure><img alt="说明" src="data:image/png;base64,..."><figcaption>截图说明</figcaption></figure></div>
  <div class="evidence-foot"><span><b>定位</b> <code>.selector</code></span><span><b>证据类型</b> PRD 证据 + 原型证据 + 运行截图</span></div>
</div>
```

左右两栏固定为“PRD 要求 → 原型实际”，中间只留一条竖向分隔线；`shots` 中按实际截图数量放一个或多个 `figure`，没有截图时整段移除并在页脚写明“运行截图未验证”及原因。PRD 摘录用 HTML 转义后展示，只保留能支撑结论的最小原文；截图以内嵌 `data:image/png;base64,...` 使用，压缩裁剪时不能删除问题所在区域。

报告整体白底、细线分区；展开区用浅灰底（`background:#fafaf9`）加左侧细线标记范围。截图单张限宽 760px、最大高度 520px，多张自动并排；正文与对照文字保持 14～14.5px，页脚辅助信息 12.5px。

标签列与正文列共用一个网格：容器设 `grid-template-columns:max-content minmax(0,1fr)`，每行用 `display:contents`，标签加 `white-space:nowrap`，确保同一组内标签和正文左边缘对齐。

## 无法截取时

- HTML 无法打开、内网资源不可用、selector 不稳定或目标状态无法复现：标记 `截图未验证`。
- 改用精确的 HTML 路径、selector/行号、源码片段或操作路径，并说明缺少哪一步运行证据。
- 不根据页面结构猜测渲染结果，也不把截图缺失自动判为 PRD 问题；结论按现有证据状态判定。

报告整体白底、细线分区；展开区用浅灰底（`background:#fafaf9`）加左侧细线标记范围。截图单张限宽 760px、最大高度 520px，多张自动并排；正文与对照文字保持 14～14.5px，页脚辅助信息 12.5px。

标签列与正文列共用一个网格：容器设 `grid-template-columns:max-content minmax(0,1fr)`，每行用 `display:contents`，标签加 `white-space:nowrap`，确保同一组内标签和正文左边缘对齐。
