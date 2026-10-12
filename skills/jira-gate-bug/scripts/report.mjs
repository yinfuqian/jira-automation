/**
 * report.mjs —— 把故障分析接口返回的结果渲染为自带样式的 HTML 报告。
 *
 * 报告要求：单文件、内联样式、不依赖外网资源，可直接作为 Jira 附件下载查看。
 * 所有动态内容一律转义，避免日志或模型输出破坏 HTML 结构。
 */

/** HTML 转义 */
export function escapeHtml(value) {
  const text = value === null || value === undefined ? "" : String(value);
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** 把任意值转成可读文本（对象转 JSON） */
function toText(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.map(toText).filter(Boolean).join("\n");
  if (typeof value === "object") {
    if (value.value !== undefined) return toText(value.value);
    if (value.name !== undefined) return toText(value.name);
    if (value.text !== undefined) return toText(value.text);
    try { return JSON.stringify(value, null, 2); } catch (e) { return ""; }
  }
  return String(value);
}

/** 多行文本渲染 */
function pre(value) {
  const text = toText(value).trim();
  if (!text) return "";
  return "<pre>" + escapeHtml(text) + "</pre>";
}

/** 字符串列表渲染 */
function list(items, emptyHint = "（无）") {
  const values = (items || []).map((item) => toText(item).trim()).filter(Boolean);
  if (!values.length) return "<p class=\"muted\">" + escapeHtml(emptyHint) + "</p>";
  return "<ul>" + values.map((item) => "<li>" + escapeHtml(item) + "</li>").join("") + "</ul>";
}

/** 置信度进度条 */
function confidenceBar(confidence) {
  let value = Number(confidence);
  if (!isFinite(value)) value = 0;
  if (value > 1) value = value / 100;
  const percent = Math.max(0, Math.min(100, Math.round(value * 100)));
  return "<div class=\"confidence\"><div class=\"bar\"><span style=\"width:" + percent + "%\"></span></div><b>" + percent + "%</b></div>";
}

/** 问题明细卡片 */
function renderIssue(issue, index) {
  if (!issue || typeof issue !== "object") return "";
  const title = toText(issue.title || issue.summary || "问题 " + (index + 1));
  const parts = [];
  parts.push("<div class=\"issue\">");
  parts.push("<h4>" + escapeHtml("问题 " + (index + 1) + "：" + title) + "</h4>");
  if (issue.issue_category || issue.issue_category_label) {
    parts.push("<p class=\"muted\">分类：" + escapeHtml(toText(issue.issue_category_label || issue.issue_category)) + "</p>");
  }
  if (issue.summary && toText(issue.summary) !== title) parts.push("<p>" + escapeHtml(toText(issue.summary)) + "</p>");
  if (issue.root_cause) {
    parts.push("<h5>根因</h5>");
    parts.push(pre(issue.root_cause));
  }
  if (issue.solution) {
    parts.push("<h5>处理建议</h5>");
    parts.push(pre(issue.solution));
  }
  if (issue.confidence !== undefined && issue.confidence !== null && issue.confidence !== "") {
    parts.push("<h5>置信度</h5>");
    parts.push(confidenceBar(issue.confidence));
  }
  if (Array.isArray(issue.code_locations) && issue.code_locations.length) {
    parts.push("<h5>代码位置</h5>");
    const rows = issue.code_locations.filter((item) => item && typeof item === "object").map((item) =>
      "<tr><td>" + escapeHtml(toText(item.file)) + "</td><td>" + escapeHtml(toText(item.line)) + "</td><td>" + escapeHtml(toText(item.reason)) + "</td></tr>"
    );
    parts.push("<table><thead><tr><th>文件</th><th>行号</th><th>说明</th></tr></thead><tbody>" + rows.join("") + "</tbody></table>");
  }
  if (issue.evidence && toText(issue.evidence).trim()) {
    parts.push("<h5>证据</h5>");
    parts.push(list(issue.evidence));
  }
  if (issue.query_commands && toText(issue.query_commands).trim()) {
    parts.push("<h5>排查命令</h5>");
    parts.push(pre(issue.query_commands));
  }
  if (issue.fix_commands && toText(issue.fix_commands).trim()) {
    parts.push("<h5>修复命令</h5>");
    parts.push(pre(issue.fix_commands));
  }
  parts.push("</div>");
  return parts.join("");
}

/** 单个分析任务的章节 */
function renderTask(task, index) {
  const result = (task && task.result) || {};
  const label = toText((task && task.label) || "分析任务 " + (index + 1));
  const conclusion = result.issue_conclusion || {};
  const evidence = result.analysis_evidence || {};
  const parts = [];
  parts.push("<section class=\"task\">");
  parts.push("<h3>" + escapeHtml(label) + "（task_id: " + escapeHtml(toText(task && task.taskId)) + "）</h3>");
  if (task && task.error) {
    parts.push("<p class=\"error\">任务失败：" + escapeHtml(toText(task.error)) + "</p>");
  }
  parts.push("<h4>结论</h4>");
  parts.push("<p class=\"lead\">" + escapeHtml(toText(conclusion.conclusion_summary) || "（模型未返回一句话结论）") + "</p>");
  const meta = [];
  if (conclusion.issue_category_label) meta.push("问题分类：" + toText(conclusion.issue_category_label));
  if (conclusion.issue_count !== undefined) meta.push("问题数量：" + toText(conclusion.issue_count));
  if (conclusion.confidence !== undefined) meta.push("整体置信度：" + Math.round(Number(conclusion.confidence || 0) * 100) + "%");
  if (result.knowledge_hit) meta.push("命中历史知识库：" + toText(result.knowledge_case_id || ""));
  if (meta.length) parts.push("<p class=\"muted\">" + escapeHtml(meta.join(" ｜ ")) + "</p>");
  if (conclusion.root_cause) {
    parts.push("<h4>根因判断</h4>");
    parts.push(pre(conclusion.root_cause));
  }
  if (conclusion.solution) {
    parts.push("<h4>处理建议</h4>");
    parts.push(pre(conclusion.solution));
  }
  if (Array.isArray(conclusion.issues) && conclusion.issues.length) {
    parts.push("<h4>问题明细</h4>");
    parts.push(conclusion.issues.map((issue, issueIndex) => renderIssue(issue, issueIndex)).join(""));
  }
  if (conclusion.evidence && toText(conclusion.evidence).trim()) {
    parts.push("<h4>综合证据</h4>");
    parts.push(list(conclusion.evidence));
  }
  if (conclusion.possible_causes && typeof conclusion.possible_causes === "object") {
    const rows = Object.keys(conclusion.possible_causes)
      .map((key) => [key, conclusion.possible_causes[key]])
      .filter(([, item]) => item && item.possible)
      .map(([key, item]) =>
        "<tr><td>" + escapeHtml(key) + "</td><td>" + escapeHtml(toText(item.reason)) + "</td></tr>"
      );
    if (rows.length) {
      parts.push("<h4>可能原因</h4>");
      parts.push("<table><thead><tr><th>类型</th><th>依据</th></tr></thead><tbody>" + rows.join("") + "</tbody></table>");
    }
  }
  const stats = [];
  if (evidence.code_snippet_count !== undefined) stats.push("代码片段：" + toText(evidence.code_snippet_count));
  if (evidence.log_error_event_count !== undefined) stats.push("日志错误事件：" + toText(evidence.log_error_event_count));
  if (evidence.grouped_issue_count !== undefined) stats.push("分组问题：" + toText(evidence.grouped_issue_count));
  if (evidence.resolved_file_count !== undefined) stats.push("定位文件：" + toText(evidence.resolved_file_count));
  if (Array.isArray(evidence.detected_languages) && evidence.detected_languages.length) stats.push("日志语言：" + evidence.detected_languages.join("、"));
  if (stats.length) {
    parts.push("<h4>证据概览</h4>");
    parts.push("<p class=\"muted\">" + escapeHtml(stats.join(" ｜ ")) + "</p>");
  }
  const repositories = (result.repositories || []).filter((item) => item && typeof item === "object");
  if (repositories.length) {
    parts.push("<h4>参与分析的代码仓库</h4>");
    const rows = repositories.map((item) =>
      "<tr><td>" + escapeHtml(toText(item.role || "")) + "</td><td>" + escapeHtml(toText(item.moduleName || "")) + "</td><td>" + escapeHtml(toText(item.branchAddress || "")) + "</td><td>" + escapeHtml(toText(item.tagVersion || "")) + "</td></tr>"
    );
    parts.push("<table><thead><tr><th>角色</th><th>模块</th><th>仓库</th><th>版本</th></tr></thead><tbody>" + rows.join("") + "</tbody></table>");
  }
  if (result.image_analysis && typeof result.image_analysis === "object") {
    const image = result.image_analysis;
    parts.push("<h4>图片识别</h4>");
    if (image.summary) parts.push("<p>" + escapeHtml(toText(image.summary)) + "</p>");
    if (image.scene_summary) parts.push("<p class=\"muted\">场景：" + escapeHtml(toText(image.scene_summary)) + "</p>");
    if (image.error_message) parts.push("<p class=\"muted\">可见报错：" + escapeHtml(toText(image.error_message)) + "</p>");
    if (image.extracted_text) parts.push(pre(image.extracted_text));
    if (Array.isArray(image.candidate_apis) && image.candidate_apis.length) parts.push(list(image.candidate_apis, "（未识别到候选接口）"));
  }
  const grouped = (evidence.grouped_log_errors || []).filter((item) => item && typeof item === "object");
  if (grouped.length) {
    parts.push("<h4>日志错误分组</h4>");
    const rows = grouped.slice(0, 20).map((item) =>
      "<tr><td>" + escapeHtml(toText(item.signature || item.title || item.summary || "")) + "</td><td>" + escapeHtml(toText(item.count || (item.events || []).length || "")) + "</td></tr>"
    );
    parts.push("<table><thead><tr><th>错误特征</th><th>次数</th></tr></thead><tbody>" + rows.join("") + "</tbody></table>");
  }
  if (Array.isArray(result.code_snippets) && result.code_snippets.length) {
    parts.push("<h4>代码片段（最多展示 10 段）</h4>");
    for (const snippet of result.code_snippets.slice(0, 10)) {
      if (!snippet || typeof snippet !== "object") continue;
      parts.push("<h5>" + escapeHtml(toText(snippet.file)) + " 第 " + escapeHtml(toText(snippet.line)) + " 行</h5>");
      parts.push(pre(snippet.numbered_snippet || snippet.snippet));
    }
  }
  if (result.code_analysis) {
    parts.push("<details><summary>展开模型原始输出</summary>");
    parts.push(pre(result.code_analysis));
    parts.push("</details>");
  }
  parts.push("</section>");
  return parts.join("");
}

/** 渲染完整报告；payload 见 SKILL.md「报告结构」 */
export function renderAnalysisHtml(payload = {}) {
  const scope = payload.scope || {};
  const files = payload.files || [];
  const tasks = payload.tasks || [];
  const generatedAt = payload.generatedAt || new Date().toISOString();
  const parts = [];
  parts.push("<!DOCTYPE html>");
  parts.push("<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">");
  parts.push("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">");
  parts.push("<title>故障分析报告 " + escapeHtml(toText(payload.issueKey)) + "</title>");
  parts.push("<style>");
  parts.push([
    ":root{color-scheme:light}",
    "body{margin:0;padding:0;background:#f4f6f9;color:#1f2733;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;line-height:1.65}",
    "main{max-width:960px;margin:0 auto;padding:24px 20px 60px}",
    "header{background:#16324f;color:#fff;border-radius:14px;padding:22px 24px;margin-bottom:20px}",
    "header h1{margin:0 0 8px;font-size:20px}",
    "header a{color:#9ec7ff}",
    ".meta{color:#c7d7ea;font-size:13px;margin:2px 0}",
    "section{background:#fff;border-radius:14px;padding:18px 22px;margin-bottom:18px;box-shadow:0 1px 3px rgba(16,42,67,.08)}",
    "h2{margin:0 0 12px;font-size:17px;border-left:4px solid #2f6fd0;padding-left:10px}",
    "h3{font-size:15px;margin:18px 0 8px}",
    "h4{font-size:14px;margin:16px 0 6px;color:#2f6fd0}",
    "h5{font-size:13px;margin:12px 0 4px;color:#41556e}",
    "p{margin:6px 0;font-size:14px}",
    ".lead{font-size:15px;font-weight:600;background:#eef4ff;border-radius:8px;padding:10px 12px}",
    ".muted{color:#6b7a8d;font-size:13px}",
    ".error{color:#b3261e;font-weight:600}",
    "pre{background:#0f1b2a;color:#e6edf6;border-radius:8px;padding:12px;overflow:auto;font-size:12.5px;line-height:1.55;white-space:pre-wrap;word-break:break-word}",
    "ul{margin:6px 0 6px 18px;padding:0;font-size:14px}",
    "table{width:100%;border-collapse:collapse;margin:8px 0;font-size:13px}",
    "th,td{border:1px solid #dde4ec;padding:6px 8px;text-align:left;vertical-align:top}",
    "th{background:#f0f4f9}",
    ".issue{border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px;margin:10px 0;background:#fbfcfe}",
    ".confidence{display:flex;align-items:center;gap:8px;font-size:12px}",
    ".bar{flex:1;height:8px;background:#e3e9f1;border-radius:5px;overflow:hidden;max-width:260px}",
    ".bar span{display:block;height:100%;background:#2f6fd0}",
    "details{margin-top:10px}",
    "summary{cursor:pointer;font-size:13px;color:#2f6fd0}",
    "footer{color:#7b8798;font-size:12px;text-align:center}",
  ].join(""));
  parts.push("</style></head><body><main>");
  parts.push("<header>");
  parts.push("<h1>故障分析报告" + (payload.issueKey ? "：" + escapeHtml(toText(payload.issueKey)) : "") + "</h1>");
  if (payload.jiraUrl) parts.push("<p class=\"meta\">缺陷单：<a href=\"" + escapeHtml(toText(payload.jiraUrl)) + "\">" + escapeHtml(toText(payload.jiraUrl)) + "</a></p>");
  parts.push("<p class=\"meta\">生成时间：" + escapeHtml(toText(generatedAt)) + "</p>");
  const scopeBits = [
    scope.productName ? "产品：" + toText(scope.productName) : "",
    scope.moduleName ? "模块：" + toText(scope.moduleName) : "",
    scope.branchAddress ? "仓库：" + toText(scope.branchAddress) : "",
    scope.tagVersion ? "版本：" + toText(scope.tagVersion) : "",
    (scope.relatedModules || []).filter((item) => item && item.common).length
      ? "引用公共组件：" + (scope.relatedModules || []).filter((item) => item && item.common).map((item) => toText(item.moduleName)).join("、")
      : "",
  ].filter(Boolean);
  if (scopeBits.length) parts.push("<p class=\"meta\">" + escapeHtml(scopeBits.join(" ｜ ")) + "</p>");
  parts.push("</header>");

  parts.push("<section><h2>分析输入</h2>");
  if (files.length) {
    const rows = files.map((file) =>
      "<tr><td>" + escapeHtml(toText(file.filename)) + "</td><td>" + escapeHtml(toText(file.kind)) + "</td><td>" + escapeHtml(toText(file.size !== undefined ? Math.round(Number(file.size) / 1024) + " KB" : "")) + "</td></tr>"
    );
    parts.push("<table><thead><tr><th>文件</th><th>类型</th><th>大小</th></tr></thead><tbody>" + rows.join("") + "</tbody></table>");
  } else {
    parts.push("<p class=\"muted\">（未提供附件）</p>");
  }
  if (Array.isArray(payload.notes) && payload.notes.length) {
    parts.push("<h4>说明</h4>");
    parts.push(list(payload.notes));
  }
  parts.push("</section>");

  if (!tasks.length) {
    parts.push("<section><h2>分析结果</h2><p class=\"error\">没有可用的分析结果，请检查附件与故障分析服务状态。</p></section>");
  }
  for (let index = 0; index < tasks.length; index += 1) {
    parts.push(renderTask(tasks[index], index));
  }

  parts.push("<footer>本报告由故障分析工具自动生成，结论由模型推理得出，请结合日志与代码人工复核后再关闭缺陷单。</footer>");
  parts.push("</main></body></html>");
  return parts.join("\n");
}
