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

function clipText(value, maxChars = 1400) {
  const text = toText(value).trim();
  if (!text || text.length <= maxChars) return text;
  return text.slice(0, maxChars).trimEnd() + "\n...（内容已截断）";
}

function clipLines(value, maxLines = 40, maxChars = 6000) {
  const lines = toText(value).trim().split(/\r?\n/).slice(0, maxLines);
  let text = lines.join("\n");
  let truncated = toText(value).trim().split(/\r?\n/).length > maxLines;
  if (text.length > maxChars) {
    text = text.slice(0, maxChars).trimEnd();
    truncated = true;
  }
  return truncated ? text + "\n...（已截断）" : text;
}

function statement(value, maxChars = 1400) {
  const text = clipText(value, maxChars);
  return text ? "<p class=\"statement\">" + escapeHtml(text) + "</p>" : "";
}

function codeBlock(value) {
  const text = toText(value).trim();
  return text ? "<pre class=\"code\">" + escapeHtml(text) + "</pre>" : "";
}

function usefulCodeLocations(issue) {
  const seen = new Set();
  return (issue && Array.isArray(issue.code_locations) ? issue.code_locations : [])
    .filter((item) => item && typeof item === "object")
    .filter((item) => {
      const file = toText(item.file).trim();
      return file && !/^未提供|^无相关|^无法定位/.test(file);
    })
    .filter((item) => {
      const key = toText(item.file).trim() + ":" + toText(item.line).trim();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}

function selectPrimaryIssue(issues) {
  const values = (issues || []).filter((item) => item && typeof item === "object");
  return values.find((item) => {
    const category = toText(item.issue_category || item.issue_category_label).toLowerCase();
    return category !== "unknown" && category !== "未知" && (item.root_cause || item.quick_solution || item.code_solution || item.solution || usefulCodeLocations(item).length);
  }) || values[0] || null;
}

function readSolution(issue, conclusion, key) {
  const aliases = key === "quick_solution"
    ? [key, "quick_mitigation", "workaround", "temporary_solution"]
    : [key, "code_fix", "permanent_solution", "code_change_suggestion"];
  for (const source of [issue, conclusion]) {
    if (!source || typeof source !== "object") continue;
    for (const alias of aliases) {
      const value = toText(source[alias]).trim();
      if (value) return value;
    }
  }
  return "";
}

function renderSolutionSection(title, hint, value, className) {
  return "<div class=\"solution-block " + className + "\">"
    + "<h3>" + escapeHtml(title) + "</h3>"
    + "<p class=\"solution-hint\">" + escapeHtml(hint) + "</p>"
    + (value === null ? "" : value ? statement(value, 1800) : "<p class=\"notice\">本次分析未单独给出这一项，请结合原因和代码位置人工确认。</p>")
    + "</div>";
}

function normalizeSteps(steps, fallbackText) {
  const values = Array.isArray(steps)
    ? steps.map((item) => toText(item).trim()).filter(Boolean)
    : [];
  if (values.length) return values;
  return toText(fallbackText).trim().split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
}

function renderQuickSteps(steps, fallbackText) {
  const values = normalizeSteps(steps, fallbackText);
  if (!values.length) return "<p class=\"notice\">本次分析未给出可直接执行的快速处置步骤，请结合原因和现场配置人工确认。</p>";
  return "<ol class=\"steps\">" + values.map((item) =>
    "<li><span>" + escapeHtml(item) + "</span></li>"
  ).join("") + "</ol>";
}

function normalizeCodeChanges(changes) {
  return (Array.isArray(changes) ? changes : [])
    .filter((item) => item && typeof item === "object")
    .map((item) => ({
      file: toText(item.file || item.file_path).trim(),
      line: toText(item.line).trim(),
      originalCode: toText(item.original_code || item.before_code || item.original).trim(),
      suggestedCode: toText(item.suggested_code || item.after_code || item.suggested).trim(),
      note: toText(item.note || item.comment || item.reason).trim(),
    }))
    .filter((item) => item.file || item.originalCode || item.suggestedCode);
}

function renderCodeChanges(changes, fallbackText) {
  const values = normalizeCodeChanges(changes);
  if (!values.length) {
    return (fallbackText ? statement(fallbackText, 1800) : "")
      + "<p class=\"notice\">本次分析未返回可核验的修改前后代码。请按上方代码位置人工核对；在此之前不要把文字建议直接作为补丁上线。</p>";
  }
  const overview = toText(fallbackText).trim()
    ? "<p class=\"code-overview\"><strong>修改思路：</strong>" + escapeHtml(clipText(fallbackText, 1200)) + "</p>"
    : "";
  return overview + values.map((change, index) =>
    "<div class=\"code-change\">"
      + "<div class=\"code-change-title\"><strong>修改 " + (index + 1) + "</strong><code>"
      + escapeHtml(change.file || "未标注文件") + (change.line ? ":" + escapeHtml(change.line) : "") + "</code></div>"
      + (change.note ? "<p class=\"code-note\"><strong>备注：</strong>" + escapeHtml(change.note) + "</p>" : "")
      + "<div class=\"diff-grid\">"
      + "<div><div class=\"diff-label before\">修改前</div>" + codeBlock(change.originalCode || "（当前版本未返回原文，请人工核对）") + "</div>"
      + "<div><div class=\"diff-label after\">建议修改后</div>" + codeBlock(change.suggestedCode || "（当前版本未返回建议代码）") + "</div>"
      + "</div></div>"
  ).join("");
}

function renderCodeLocations(issue) {
  const locations = usefulCodeLocations(issue).slice(0, 6);
  if (!locations.length) return "<p class=\"notice\">分析结果未定位到明确代码文件，需要结合修复建议人工核实。</p>";
  const rows = locations.map((item) =>
    "<tr><td><code>" + escapeHtml(toText(item.file)) + "</code></td><td>" + escapeHtml(toText(item.line) || "未标注") + "</td><td>" + escapeHtml(clipText(item.reason, 260) || "相关代码位置") + "</td></tr>"
  );
  return "<table><thead><tr><th>代码文件</th><th>行号</th><th>用途</th></tr></thead><tbody>" + rows.join("") + "</tbody></table>";
}

function renderCodeSnippets(snippets, issue) {
  const locations = usefulCodeLocations(issue);
  const pathTokens = locations.map((item) => toText(item.file).trim().toLowerCase()).filter(Boolean);
  const basenameTokens = pathTokens.map((item) => item.split(/[\\/]/).pop()).filter((item) => /\.[a-z0-9]+$/i.test(item));
  const values = (snippets || [])
    .filter((item) => item && typeof item === "object")
    .filter((item) => {
      if (!pathTokens.length) return false;
      const file = toText(item.file).trim().toLowerCase();
      const basename = file.split(/[\\/]/).pop();
      return pathTokens.some((token) => file.includes(token) || token.includes(file) || basenameTokens.some((name) => name === basename || file.endsWith(name)));
    })
    .slice(0, 3);
  if (!values.length) return "<p class=\"notice\">本次分析未返回与主要代码位置匹配的代码片段，请根据上方文件与行号人工核实。</p>";
  return values.map((snippet, index) =>
    "<div class=\"snippet\"><div class=\"snippet-title\">" + escapeHtml((index + 1) + ". " + (toText(snippet.file) || "未标注文件") + " 第 " + (toText(snippet.line) || "?") + " 行") + "</div>"
      + codeBlock(clipLines(snippet.numbered_snippet || snippet.snippet)) + "</div>"
  ).join("");
}

function renderOtherIssues(issues, primaryIssue) {
  const values = (issues || []).filter((item) => item && item !== primaryIssue && typeof item === "object").slice(0, 10);
  if (!values.length) return "";
  const rows = values.map((item, index) =>
    "<tr><td>" + (index + 1) + "</td><td>" + escapeHtml(clipText(item.title || item.summary || "未命名观察项", 140)) + "</td><td>" + escapeHtml(clipText(item.summary || item.root_cause, 180)) + "</td></tr>"
  );
  return "<details class=\"secondary\"><summary>其他观察项（" + values.length + "，仅核实，不代表都需修复）</summary>"
    + "<table><thead><tr><th>#</th><th>观察项</th><th>简要说明</th></tr></thead><tbody>" + rows.join("") + "</tbody></table></details>";
}

/** 单个分析任务的章节 */
function renderTask(task, index) {
  const result = (task && task.result) || {};
  const label = toText((task && task.label) || "分析任务 " + (index + 1));
  const conclusion = result.issue_conclusion || {};
  const evidence = result.analysis_evidence || {};
  const issues = Array.isArray(conclusion.issues) ? conclusion.issues : [];
  const primaryIssue = selectPrimaryIssue(issues) || {
    title: conclusion.conclusion_summary,
    root_cause: conclusion.root_cause,
    quick_solution: conclusion.quick_solution,
    quick_steps: conclusion.quick_steps,
    code_solution: conclusion.code_solution,
    code_changes: conclusion.code_changes,
    solution: conclusion.solution,
    confidence: conclusion.confidence,
    code_locations: conclusion.code_locations || [],
  };
  const problemTitle = clipText((primaryIssue && (primaryIssue.title || primaryIssue.summary)) || conclusion.conclusion_summary || "未返回明确问题", 180);
  const problemSummary = clipText((primaryIssue && primaryIssue.summary) || conclusion.conclusion_summary, 1000);
  const rootCause = (primaryIssue && primaryIssue.root_cause) || conclusion.root_cause;
  const quickSolution = readSolution(primaryIssue, conclusion, "quick_solution");
  let codeSolution = readSolution(primaryIssue, conclusion, "code_solution");
  const quickSteps = (primaryIssue && primaryIssue.quick_steps) || conclusion.quick_steps || [];
  const codeChanges = (primaryIssue && primaryIssue.code_changes) || conclusion.code_changes || [];
  const legacySolution = toText((primaryIssue && primaryIssue.solution) || conclusion.solution).trim();
  if (!codeSolution && legacySolution) codeSolution = legacySolution;
  const parts = [];
  parts.push("<section class=\"task\" id=\"task-" + (index + 1) + "\">");
  parts.push("<div class=\"task-heading\"><div><p class=\"eyebrow\">" + escapeHtml(label) + "</p><h2>核心问题</h2></div>");
  if (conclusion.issue_category_label) parts.push("<span class=\"badge\">" + escapeHtml(toText(conclusion.issue_category_label)) + "</span>");
  parts.push("</div>");
  if (task && task.error) {
    parts.push("<p class=\"error\">任务失败：" + escapeHtml(toText(task.error)) + "</p>");
  }
  parts.push("<h3 class=\"problem-title\">" + escapeHtml(problemTitle) + "</h3>");
  if (problemSummary && problemSummary !== problemTitle) parts.push(statement(problemSummary, 1000));
  const meta = [];
  if (conclusion.issue_count !== undefined) meta.push("识别问题：" + toText(conclusion.issue_count) + " 项");
  if (conclusion.confidence !== undefined) meta.push("置信度：" + Math.round(Number(conclusion.confidence || 0) * 100) + "%");
  if (result.knowledge_hit) meta.push("命中历史知识：" + toText(result.knowledge_case_id || "是"));
  if (result.image_analysis && result.image_analysis.error_message) meta.push("图片可见报错：" + clipText(result.image_analysis.error_message, 220));
  if (meta.length) parts.push("<p class=\"meta-line\">" + escapeHtml(meta.join(" ｜ ")) + "</p>");

  if (rootCause) {
    parts.push("<h2 class=\"block-title\">原因判断</h2>");
    parts.push(statement(rootCause, 1400));
  }
  parts.push("<h2 class=\"block-title\">解决办法</h2>");
  parts.push(renderSolutionSection(
    "1. 快速解决方案（立即规避）",
    "优先确认能否通过修改配置、参数、开关、回滚或人工补数恢复正常；本项不是长期修复。",
    null,
    "quick",
  ));
  parts.push("<div class=\"solution-body quick-body\">" + renderQuickSteps(quickSteps, quickSolution) + "</div>");
  parts.push(renderSolutionSection(
    "2. 代码修改建议（长期修复）",
    "逐项核对文件与行号；“修改前”必须来自当前 Tag，“建议修改后”需人工评审并验证。",
    null,
    "code-fix",
  ));
  parts.push("<div class=\"solution-body code-body\">" + renderCodeChanges(codeChanges, codeSolution) + "</div>");
  if (primaryIssue && primaryIssue.confidence !== undefined) {
    parts.push("<p class=\"meta-line\">问题定位置信度：" + Math.round(Number(primaryIssue.confidence || 0) * 100) + "%</p>");
  }

  parts.push("<h2 class=\"block-title\">代码文件位置</h2>");
  parts.push(renderCodeLocations(primaryIssue));
  parts.push("<h2 class=\"block-title\">关键代码片段</h2>");
  parts.push(renderCodeSnippets(result.code_snippets, primaryIssue));
  parts.push(renderOtherIssues(issues, primaryIssue));

  const stats = [];
  if (evidence.code_snippet_count !== undefined) stats.push("代码片段：" + toText(evidence.code_snippet_count));
  if (evidence.log_error_event_count !== undefined) stats.push("日志错误事件：" + toText(evidence.log_error_event_count));
  if (evidence.grouped_issue_count !== undefined) stats.push("分组问题：" + toText(evidence.grouped_issue_count));
  if (evidence.resolved_file_count !== undefined) stats.push("定位文件：" + toText(evidence.resolved_file_count));
  if (Array.isArray(evidence.detected_languages) && evidence.detected_languages.length) stats.push("日志语言：" + evidence.detected_languages.join("、"));
  const repositories = (result.repositories || []).filter((item) => item && typeof item === "object");
  if (stats.length || repositories.length) {
    parts.push("<details class=\"technical\"><summary>技术信息与证据概览</summary>");
    if (stats.length) parts.push("<p class=\"meta-line\">" + escapeHtml(stats.join(" ｜ ")) + "</p>");
  }
  if (repositories.length) {
    const rows = repositories.map((item) =>
      "<tr><td>" + escapeHtml(toText(item.role || "")) + "</td><td>" + escapeHtml(toText(item.moduleName || "")) + "</td><td>" + escapeHtml(toText(item.branchAddress || "")) + "</td><td>" + escapeHtml(toText(item.tagVersion || "")) + "</td></tr>"
    );
    parts.push("<table><thead><tr><th>角色</th><th>模块</th><th>仓库</th><th>版本</th></tr></thead><tbody>" + rows.join("") + "</tbody></table>");
  }
  if (stats.length || repositories.length) parts.push("</details>");
  if (task && task.taskId) parts.push("<p class=\"task-id\">task_id: " + escapeHtml(toText(task.taskId)) + "</p>");
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
    "main{max-width:920px;margin:0 auto;padding:24px 20px 60px}",
    "header{background:#16324f;color:#fff;border-radius:10px;padding:22px 24px;margin-bottom:20px}",
    "header h1{margin:0 0 8px;font-size:20px}",
    "header a{color:#9ec7ff}",
    ".meta{color:#c7d7ea;font-size:13px;margin:2px 0}",
    "section{background:#fff;border-radius:10px;padding:20px 24px;margin-bottom:18px;box-shadow:0 1px 3px rgba(16,42,67,.08)}",
    "h2{margin:0 0 12px;font-size:17px;border-left:4px solid #2f6fd0;padding-left:10px}",
    "h3{font-size:15px;margin:18px 0 8px}",
    "h4{font-size:14px;margin:16px 0 6px;color:#2f6fd0}",
    "h5{font-size:13px;margin:12px 0 4px;color:#41556e}",
    "p{margin:6px 0;font-size:14px}",
    ".muted{color:#6b7a8d;font-size:13px}",
    ".error{color:#b3261e;font-weight:600}",
    ".eyebrow{font-size:12px;letter-spacing:0;text-transform:uppercase;color:#6b7a8d;margin:0}",
    ".task-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}",
    ".task-heading h2{margin:4px 0 16px}",
    ".badge{background:#eef4ff;color:#245ba9;border-radius:999px;padding:4px 10px;font-size:12px;font-weight:600;white-space:nowrap}",
    ".problem-title{margin:2px 0 8px;font-size:20px;line-height:1.45;color:#142a42}",
    ".block-title{margin:26px 0 8px;font-size:15px;border-left-color:#8aa6c7}",
    ".solution-block{margin:14px 0 18px;padding:14px 16px;border-left:4px solid #2f6fd0;background:#f4f8fe}",
    ".solution-block.quick{border-left-color:#c78514;background:#fff8e8}",
    ".solution-block h3{margin:0 0 4px;font-size:15px}",
    ".solution-hint{margin:0 0 8px;color:#637287;font-size:12.5px}",
    ".solution-body{margin:-8px 0 20px;padding:14px 16px;border-left:4px solid #2f6fd0;background:#f4f8fe}",
    ".solution-body.quick-body{border-left-color:#c78514;background:#fff8e8}",
    ".steps{margin:0;padding-left:24px}",
    ".steps li{margin:0 0 10px;padding-left:4px;font-size:14px;line-height:1.75}",
    ".steps li::marker{font-weight:700;color:#8a5a00}",
    ".code-change{margin:0 0 18px;padding-bottom:18px;border-bottom:1px solid #dce6f2}",
    ".code-overview{margin:0 0 14px;font-size:13.5px;line-height:1.75;color:#334a63}",
    ".code-change:last-child{margin-bottom:0;padding-bottom:0;border-bottom:0}",
    ".code-change-title{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:8px;font-size:13px}",
    ".code-note{margin:0 0 10px;padding:8px 10px;background:#fff;border:1px solid #dce6f2;font-size:13px;color:#3f5369}",
    ".diff-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}",
    ".diff-label{font-size:12px;font-weight:700;margin-bottom:5px}",
    ".diff-label.before{color:#9f2f27}",
    ".diff-label.after{color:#1d6b3b}",
    "@media(max-width:720px){.diff-grid{grid-template-columns:1fr}}",
    ".statement{white-space:pre-wrap;word-break:break-word;font-size:14.5px;line-height:1.8;color:#26384d}",
    ".meta-line{color:#5f7085;font-size:12.5px;line-height:1.7}",
    ".notice{background:#f6f8fb;border-left:3px solid #9aabc0;color:#52657b;padding:9px 12px;font-size:13px}",
    "pre,.code{background:#0f1b2a;color:#e6edf6;border-radius:7px;padding:12px;overflow:auto;font-size:12.5px;line-height:1.6;white-space:pre-wrap;word-break:break-word}",
    "ul{margin:6px 0 6px 18px;padding:0;font-size:14px}",
    "table{width:100%;border-collapse:collapse;margin:8px 0;font-size:13px}",
    "th,td{border:1px solid #dde4ec;padding:6px 8px;text-align:left;vertical-align:top}",
    "th{background:#f0f4f9}",
    "code{font-family:Consolas,'SFMono-Regular',monospace;font-size:12.5px;color:#214e86;background:#eef3f9;padding:1px 4px;border-radius:4px}",
    ".snippet{margin:12px 0 18px}",
    ".snippet-title{font-family:Consolas,'SFMono-Regular',monospace;font-size:12.5px;color:#41556e;margin-bottom:6px}",
    "details{margin-top:10px}",
    "summary{cursor:pointer;font-size:13px;color:#2f6fd0}",
    ".secondary,.technical{margin-top:24px;border-top:1px solid #e5ebf2;padding-top:12px}",
    ".task-id{color:#8a97a7;font-size:11px;font-family:Consolas,'SFMono-Regular',monospace;margin-top:18px}",
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
    const imageCount = files.filter((file) => toText(file.kind) === "图片").length;
    const logCount = files.filter((file) => toText(file.kind) === "日志").length;
    const totalKb = Math.round(files.reduce((sum, file) => sum + (Number(file.size) || 0), 0) / 1024);
    const counts = [
      logCount ? "日志 " + logCount + " 个" : "",
      imageCount ? "图片 " + imageCount + " 张" : "",
      totalKb ? "合计约 " + totalKb + " KB" : "",
    ].filter(Boolean);
    parts.push("<p class=\"meta-line\">" + escapeHtml(counts.join(" ｜ ")) + "</p>");
  } else {
    parts.push("<p class=\"muted\">（未提供附件）</p>");
  }
  if (Array.isArray(payload.notes) && payload.notes.length) {
    parts.push("<p class=\"meta-line\">处理说明：" + escapeHtml(clipText(payload.notes.slice(0, 3).join("；"), 500)) + "</p>");
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
