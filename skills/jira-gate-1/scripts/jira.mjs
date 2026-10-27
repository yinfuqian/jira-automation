/**
 * jira.mjs —— Jira 需求准入预检客户端（在 Codex 的 node_repl 中运行）
 *
 * 运行环境约束（node_repl 沙箱）：
 *   - 没有 `process`：环境变量一律经 readEnv() 探测，缺失时回退到令牌文件与调用方入参
 *   - 不支持静态 import，模块内一律使用 await import()
 *   - 网络只能通过本模块内的 fetch 访问（PowerShell 沙箱下 TLS 被拦截）
 *
 * 典型用法（在 node_repl 中）：
 *   const j = await import("file:///<SKILL_DIR>/scripts/jira.mjs");   // <SKILL_DIR> 为本 skill 的安装目录
 *   const issue = await j.getIssue("CALL-1446");
 *   nodeRepl.write(j.renderIssueMarkdown(issue));
 */
export const DEFAULT_BASE_URL = "https://jira.in.wezhuiyi.com";

/** 允许的凭证文件候选位置（相对 skill 目录与 ~/.codex） */
const TOKEN_FILE_NAMES = [".jira-token", ".jira-token.txt", "jira-token.txt"];

/** 服务端锁定标记：为 "1" 时技能只认环境变量，禁止回落到令牌文件与命令行入参 */
export const ENV_LOCK_FLAG = "SKILLRUN_ENV_LOCKED";

/** 是否处于服务端锁定模式（容器内由后端注入 SKILLRUN_ENV_LOCKED=1） */
export function envLocked() {
  return readEnv(ENV_LOCK_FLAG) === "1";
}

/** 去掉末尾斜杠并转小写，用于判断两个地址是否同一个站点 */
function normalizeUrlForCompare(value) {
  return String(value || "").trim().replace(/\/+$/, "").toLowerCase();
}

/** 锁定模式下的权威地址：未注入时直接报错，绝不回落到内置默认站点 */
function lockedBaseUrl() {
  const value = readEnv("JIRA_BASE_URL");
  if (!value) {
    throw new Error("运行环境已锁定，但服务端未注入 JIRA_BASE_URL；请检查 .env 配置后重启 api/worker。");
  }
  return value.replace(/\/+$/, "");
}

/** 锁定模式下校验访问目标就是 JIRA_BASE_URL 配置的站点 */
function assertLockedTarget(target) {
  const expected = lockedBaseUrl();
  let origin = "";
  try {
    origin = new URL(String(target)).origin;
  } catch (e) {
    throw new Error("运行环境已锁定，但目标地址不是合法 URL：" + target);
  }
  if (normalizeUrlForCompare(origin) !== normalizeUrlForCompare(expected)) {
    throw new Error("运行环境已锁定：只允许访问 JIRA_BASE_URL 配置的站点 " + expected + "，已拒绝访问 " + origin + "。");
  }
}

/** 锁定模式下校验调用方传入的 Jira 链接指向配置站点 */
export function assertLockedIssueInput(input) {
  if (!envLocked()) return;
  const text = String(input || "").trim();
  if (!/^https?:\/\//i.test(text)) return;
  assertLockedTarget(text);
}


/** 安全读取环境变量：node_repl 沙箱里没有 process，缺失时返回空字符串 */
function readEnv(name) {
  try {
    if (typeof process === "undefined" || !process || !process.env) return "";
    const value = process.env[name];
    return value ? String(value).trim() : "";
  } catch (e) {
    return "";
  }
}

/** 门禁字段默认候选名（按优先级匹配）；可用 config.json 的 gateResultField / gateDifficultyField / gateCheckedAtField 覆盖 */
const DEFAULT_GATE_FIELD_NAMES = {
  result: ["G1准入结论", "G1需求准入结论", "需求准入结论"],
  difficulty: ["G1需求难度", "G1需求难度分级", "需求难度"],
  checkedAt: ["G1预检时间", "G1需求准入预检时间", "需求准入预检时间"],
};

// G1 只处理「故事」类型的单子；英文站点返回 Story 时视为同一种类型。
const SUPPORTED_ISSUE_TYPES = ["故事", "Story"];

/** 判断 Jira 问题类型是否在 G1 的处理范围内（只认「故事」/ Story）。 */
export function isSupportedIssueType(issueType) {
  return SUPPORTED_ISSUE_TYPES.includes(String(issueType || "").trim());
}

/**
 * 类型预检：在读取附件、调用其它技能或写 Jira 之前先确认单据类型。
 * 返回 supported=false 时调用方必须立即停止，不能继续做任何写操作。
 */
export async function precheckIssueType(keyOrUrl, options = {}) {
  const issue = options.issue || await getIssue(keyOrUrl, options);
  const issueType = String(issue.issueType || "").trim();
  return {
    ok: true,
    key: issue.key,
    issueType,
    expected: "故事",
    supported: isSupportedIssueType(issueType),
    status: issue.status,
    summary: issue.summary,
  };
}

/** 从任意输入中解析 Jira 问题 Key */
export function parseIssueKey(input) {
  if (!input) return null;
  const raw = String(input).trim();
  let decoded = raw;
  try { decoded = decodeURIComponent(raw); } catch (e) { decoded = raw; }
  const haystack = decoded === raw ? raw : `${raw}\n${decoded}`;
  const patterns = [
    /^([A-Z][A-Z0-9_]*-\d+)$/i,
    /\/browse\/([A-Z][A-Z0-9_]*-\d+)/i,
    /[?&]selectedIssue=([A-Z][A-Z0-9_]*-\d+)/i,
    /[?&](?:key|issue)=([A-Z][A-Z0-9_]*-\d+)/i,
    /(?:^|[^A-Za-z0-9_])([A-Z][A-Z0-9_]{1,20}-\d{1,8})(?![A-Za-z0-9_-])/,
  ];
  for (const re of patterns) {
    const m = haystack.match(re);
    if (m) return m[1].toUpperCase();
  }
  return null;
}

/** 返回 skill 目录的绝对路径（失败时返回 null） */
export function skillDir() {
  try {
    const raw = import.meta.url;
    // 末尾可能带 ?v=时间戳 之类的查询串（用于 node_repl 强制重新加载），一并容忍。
    const m = String(raw).match(/^file:\/\/\/(.+?)\/scripts\/jira\.mjs(\?.*)?$/);
    if (!m) return null;
    let dir = m[1];
    try { dir = decodeURIComponent(dir); } catch (e) { /* 解码失败时保留原样 */ }
    // file:///C:/... 与 file:///home/... 解析后都用正斜杠，Windows 与 Linux 的 fs/path 都接受，
    // 因此不再替换成反斜杠（旧实现在 Linux 容器里会拼出无效路径）。
    return dir;
  } catch (e) { /* ignore */ }
  return null;
}

/** 读取第一个可用的令牌文件，返回 { token, source, baseUrl, auth }
 *  支持三种写法：
 *    1) 纯令牌文本（单行）
 *    2) JIRA_PAT=xxx
 *    3) JSON 配置：{"token":"...","baseUrl":"...","auth":"bearer|basic","username":"..."}
 */
export async function resolveToken(explicit) {
  if (envLocked()) {
    // 锁定模式：令牌只认服务端注入的 JIRA_TOKEN，不读令牌文件、不认命令行入参。
    const envToken = readEnv("JIRA_TOKEN");
    const explicitToken = explicit ? String(explicit).trim() : "";
    if (explicitToken && explicitToken !== envToken) {
      throw new Error("运行环境已锁定：不允许用 --token 覆盖服务端注入的 JIRA_TOKEN。");
    }
    return {
      token: envToken || null,
      source: envToken ? "环境变量 JIRA_TOKEN（服务端锁定）" : null,
      baseUrl: readEnv("JIRA_BASE_URL") || null,
    };
  }
  if (explicit) return { token: String(explicit).trim(), source: "argument" };
  const fs = await import("node:fs");
  const path = await import("node:path");
  const normalize = (p) => { try { return path.resolve(p); } catch (e) { return p; } };
  const dir = skillDir();
  const candidates = [];
  if (dir) {
    // 统一用正斜杠拼接：Windows 与 Linux 下 path.resolve 都能正确解析。
    for (const name of TOKEN_FILE_NAMES) candidates.push(normalize(`${dir}/${name}`));
    for (const name of TOKEN_FILE_NAMES) candidates.push(normalize(`${dir}/../${name}`));
    candidates.push(normalize(`${dir}/../../jira-token.txt`));
    candidates.push(normalize(`${dir}/../../jira/token.txt`));
    candidates.push(normalize(`${dir}/../../jira/config.json`));
  }
  let fromFile = null;
  for (const file of candidates) {
    try {
      if (!fs.existsSync(file)) continue;
      const raw = fs.readFileSync(file, "utf8");
      const text = raw.trim();
      if (!text) continue;
      if (text.startsWith("{")) {
        const cfg = JSON.parse(text);
        if (cfg.token) {
          fromFile = {
            token: String(cfg.token).trim(),
            source: file,
            baseUrl: cfg.baseUrl,
            auth: cfg.auth,
            username: cfg.username,
          };
          break;
        }
        continue;
      }
      const firstLine = text.split(/\r?\n/).find((line) => line.trim()) || "";
      const token = firstLine.replace(/^JIRA_PAT\s*=\s*/i, "").trim();
      if (token) {
        fromFile = { token, source: file };
        break;
      }
    } catch (e) { /* 忽略单个候选文件的读取错误 */ }
  }
  // 环境变量优先于令牌文件，与 jira-cli.mjs 的优先级保持一致：容器内 JIRA_BASE_URL 能压住
  // 令牌文件里的地址，避免误连生产 Jira。node_repl 沙箱没有 process，readEnv 返回空值，
  // 此时行为与原先完全一致（只用令牌文件）。
  const envToken = readEnv("JIRA_TOKEN");
  const envBaseUrl = readEnv("JIRA_BASE_URL");
  if (envToken) {
    return {
      token: envToken,
      source: "环境变量 JIRA_TOKEN",
      baseUrl: envBaseUrl || (fromFile && fromFile.baseUrl),
      auth: fromFile && fromFile.auth,
      username: fromFile && fromFile.username,
    };
  }
  if (envBaseUrl) {
    const cred = fromFile || { token: null, source: null, checked: candidates };
    return {
      token: cred.token,
      source: cred.source,
      baseUrl: envBaseUrl,
      auth: cred.auth,
      username: cred.username,
      checked: cred.checked,
    };
  }
  if (fromFile) return fromFile;
  return { token: null, source: null, checked: candidates };
}

/** 组装认证头：默认 Bearer；配置 auth=basic 时用 Basic（兼容只支持 Basic 的 Jira） */
export function buildAuthHeader(cred) {
  const mode = String(cred.auth || "bearer").toLowerCase();
  if (mode === "basic") {
    const user = cred.username || "";
    const raw = `${user}:${cred.token}`;
    if (typeof Buffer !== "undefined") return `Basic ${Buffer.from(raw, "utf8").toString("base64")}`;
    return `Basic ${btoa(unescape(encodeURIComponent(raw)))}`;
  }
  return `Bearer ${cred.token}`;
}

/** 统一发起 REST 请求 */
export async function request(method, apiPath, options = {}) {
  const cred = await resolveToken(options.token || null);
  const locked = envLocked();
  let baseUrl;
  if (locked) {
    // 锁定模式：只允许访问服务端配置的站点，传入其它地址直接拒绝。
    baseUrl = lockedBaseUrl();
    if (options.baseUrl && normalizeUrlForCompare(options.baseUrl) !== normalizeUrlForCompare(baseUrl)) {
      throw new Error("运行环境已锁定：只允许访问 JIRA_BASE_URL 配置的站点 " + baseUrl + "。");
    }
  } else {
    baseUrl = (options.baseUrl || cred.baseUrl || readEnv("JIRA_BASE_URL") || DEFAULT_BASE_URL).replace(/\/+$/, "");
  }
  const headers = { Accept: "application/json", "X-Atlassian-Token": "no-check" };
  if (cred.token) headers.Authorization = buildAuthHeader(cred);
  let payload;
  if (options.body !== undefined) {
    // FormData 用于附件上传：交给 fetch 自行生成 multipart 边界，不能手工设置 Content-Type。
    const isFormData = typeof FormData !== "undefined" && options.body instanceof FormData;
    if (isFormData) {
      payload = options.body;
    } else {
      headers["Content-Type"] = "application/json";
      payload = typeof options.body === "string" ? options.body : JSON.stringify(options.body);
    }
  }
  if (locked && /^https?:\/\//i.test(apiPath)) assertLockedTarget(apiPath);
  const target = /^https?:\/\//i.test(apiPath) ? apiPath : baseUrl + apiPath;
  const res = await fetch(target, { method, headers, body: payload, redirect: "follow" });
  if (options.raw) return res;
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
  if (!res.ok) {
    let detail = "";
    if (data && typeof data === "object") {
      detail = [].concat(data.errorMessages || [], Object.values(data.errors || {})).filter(Boolean).join("; ");
    } else if (typeof data === "string") {
      detail = data.replace(/<[^>]+>/g, " ").slice(0, 300);
    }
    const err = new Error(`Jira ${method} ${apiPath} 失败：HTTP ${res.status}${detail ? " — " + detail : ""}`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

/** 获取当前登录用户（用于确认令牌有效） */
export async function myself(options = {}) {
  return await request("GET", "/rest/api/2/myself", options);
}

/** 字段值转纯文本 */
function toPlainText(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.map(toPlainText).filter(Boolean).join("\n");
  if (typeof value === "object") {
    if (value.type === "doc" && Array.isArray(value.content)) return toPlainText(value.content);
    if (value.value !== undefined) return toPlainText(value.value);
    if (value.name !== undefined) return toPlainText(value.name);
    if (value.text !== undefined) return toPlainText(value.text);
    if (Array.isArray(value.content)) return toPlainText(value.content);
    const keys = Object.keys(value).filter((k) => !["self", "id", "disabled", "iconUrl"].includes(k));
    if (!keys.length) return "";
    try { return JSON.stringify(value); } catch (e) { return ""; }
  }
  return String(value);
}

/** 与准入判定无关、纯噪音的字段，渲染时跳过 */
const NOISE_FIELD_NAMES = ["Rank", "Global Rank", "Development", "Story point estimate", "Σ Progress"];

/** HTML 去标签，保留段落与换行 */
function htmlToText(html) {
  if (!html) return "";
  return String(html)
    .replace(/<\s*br\s*\/?\s*>/gi, "\n")
    .replace(/<\s*\/(p|div|li|tr|h[1-6])\s*>/gi, "\n")
    .replace(/<\s*li[^>]*>/gi, "- ")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

const NO_VALUE = ["", "无", "None", "null", "未设置", "-"];

/** 清理 Jira wiki 渲染标记，避免噪音干扰准入判定 */
export function cleanWikiMarkup(text) {
  if (!text) return "";
  return String(text)
    .replace(/\{\s*color(:[^}]*)?\s*\}/gi, "")
    .replace(/\{\s*(?:code|noformat|panel|quote|info|note|warning|tip|expand)(?::[^}]*)?\s*\}/gi, "")
    .replace(/\[\^([^\]]+)\]/g, "$1")
    .replace(/\{anchor:[^}]*\}/gi, "")
    .trim();
}

/** 是否视为"没有填写" */
export function isEmptyValue(value) {
  const text = toPlainText(value).trim();
  return text === "" || NO_VALUE.includes(text);
}

/** 把 Jira 原始返回规范化成便于判定的结构 */
export function normalizeIssue(raw) {
  const f = raw.fields || {};
  const names = raw.names || {};
  const baseUrl = (raw.self || "").split("/rest/")[0] || readEnv("JIRA_BASE_URL") || DEFAULT_BASE_URL;

  const customFields = {};
  for (const [id, name] of Object.entries(names)) {
    if (!id.startsWith("customfield_")) continue;
    const value = f[id];
    if (value === undefined || value === null) continue;
    customFields[name] = toPlainText(value).trim();
  }

  const pick = (user) => (user ? { name: user.name, displayName: user.displayName, email: user.emailAddress } : null);

  const links = [];
  for (const link of f.issuelinks || []) {
    const other = link.outwardIssue || link.inwardIssue;
    if (!other) continue;
    links.push({
      type: link.type ? link.type.name : "",
      direction: link.outwardIssue ? "outward" : "inward",
      label: (link.outwardIssue ? link.type.outward : link.type.inward) || "",
      key: other.key,
      summary: other.fields ? other.fields.summary : "",
      status: other.fields && other.fields.status ? other.fields.status.name : "",
    });
  }

  const descriptionRaw = toPlainText(f.description);
  const descriptionHtml = (raw.renderedFields && raw.renderedFields.description) || "";
  const description = cleanWikiMarkup(descriptionRaw || htmlToText(descriptionHtml));

  return {
    key: raw.key,
    id: raw.id,
    url: `${baseUrl}/browse/${raw.key}`,
    summary: f.summary,
    issueType: f.issuetype ? f.issuetype.name : "",
    status: f.status ? f.status.name : "",
    statusCategory: f.status && f.status.statusCategory ? f.status.statusCategory.name : "",
    priority: f.priority ? f.priority.name : "",
    resolution: f.resolution ? f.resolution.name : "",
    created: f.created,
    updated: f.updated,
    dueDate: f.duedate,
    project: f.project ? { key: f.project.key, name: f.project.name } : null,
    reporter: pick(f.reporter),
    creator: pick(f.creator),
    assignee: pick(f.assignee),
    labels: f.labels || [],
    components: (f.components || []).map((c) => c.name),
    fixVersions: (f.fixVersions || []).map((v) => v.name),
    affectedVersions: (f.versions || []).map((v) => v.name),
    description,
    descriptionHtml,
    customFields,
    timeTracking: f.timetracking || null,
    originalEstimateSeconds: f.timeoriginalestimate === undefined ? null : f.timeoriginalestimate,
    remainingEstimateSeconds: f.timeestimate === undefined ? null : f.timeestimate,
    attachments: (f.attachment || []).map((a) => ({
      id: a.id,
      filename: a.filename,
      size: a.size,
      mimeType: a.mimeType,
      created: a.created,
      author: a.author ? a.author.displayName : "",
      content: a.content,
    })),
    links,
    subtasks: (f.subtasks || []).map((s) => ({
      key: s.key,
      summary: s.fields ? s.fields.summary : "",
      status: s.fields && s.fields.status ? s.fields.status.name : "",
      assignee: s.fields && s.fields.assignee ? s.fields.assignee.displayName : "",
      issueType: s.fields && s.fields.issuetype ? s.fields.issuetype.name : "",
    })),
    comments: ((f.comment && f.comment.comments) || []).map((c) => ({
      id: c.id,
      author: c.author ? c.author.displayName : "",
      authorName: c.author ? c.author.name : "",
      created: c.created,
      updated: c.updated,
      body: toPlainText(c.body),
    })),
  };
}

/** 通过 JQL 查询以该单为父的需求子任务（部分 Jira 项目的 subtasks 字段不可用时兜底） */
export async function fetchSubtasksByParent(keyOrUrl, options = {}) {
  const key = parseIssueKey(keyOrUrl);
  assertLockedIssueInput(keyOrUrl);
  if (!key) return [];
  try {
    const jql = encodeURIComponent(`parent = ${key}`);
    const data = await request("GET", `/rest/api/2/search?jql=${jql}&fields=summary,status,assignee,issuetype&maxResults=50`, options);
    return (data.issues || []).map((s) => ({
      key: s.key,
      summary: s.fields ? s.fields.summary : "",
      status: s.fields && s.fields.status ? s.fields.status.name : "",
      assignee: s.fields && s.fields.assignee ? s.fields.assignee.displayName : "",
      issueType: s.fields && s.fields.issuetype ? s.fields.issuetype.name : "",
    }));
  } catch (e) {
    return [];
  }
}

/** 获取并规范化单个需求单 */
export async function getIssue(keyOrUrl, options = {}) {
  const key = parseIssueKey(keyOrUrl);
  assertLockedIssueInput(keyOrUrl);
  if (!key) throw new Error(`无法从输入中解析 Jira 问题 Key：${keyOrUrl}`);
  const data = await request("GET", `/rest/api/2/issue/${encodeURIComponent(key)}?expand=names,renderedFields,schema`, options);
  const issue = normalizeIssue(data);
  if (!issue.subtasks.length) {
    const found = await fetchSubtasksByParent(key, options);
    if (found.length) issue.subtasks = found;
  }
  const commentMeta = (data.fields && data.fields.comment) || {};
  if (commentMeta.total && commentMeta.total > issue.comments.length) {
    const all = await request("GET", `/rest/api/2/issue/${encodeURIComponent(key)}/comment`, options);
    if (all && Array.isArray(all.comments)) {
      issue.comments = all.comments.map((c) => ({
        id: c.id,
        author: c.author ? c.author.displayName : "",
        authorName: c.author ? c.author.name : "",
        created: c.created,
        updated: c.updated,
        body: toPlainText(c.body),
      }));
    }
  }
  return issue;
}

/** 把规范化后的需求单渲染为便于判定的 Markdown 摘要 */
export function renderIssueMarkdown(issue, options = {}) {
  const maxDesc = options.maxDescriptionChars || 8000;
  const maxComments = options.maxComments || 8;
  const maxCommentChars = options.maxCommentChars || 800;
  const lines = [];
  const kv = (k, v) => lines.push(`- ${k}：${isEmptyValue(v) ? "（空）" : toPlainText(v)}`);

  lines.push(`# ${issue.key} ${issue.summary}`);
  lines.push("");
  lines.push("## 基本字段");
  kv("链接", issue.url);
  kv("类型", issue.issueType);
  kv("状态", issue.status);
  kv("优先级", issue.priority);
  kv("解决结果", issue.resolution);
  kv("经办人", issue.assignee ? issue.assignee.displayName : "");
  kv("报告人", issue.reporter ? issue.reporter.displayName : "");
  kv("所属项目", issue.project ? `${issue.project.name}（${issue.project.key}）` : "");
  kv("产品归属/客户等自定义字段", "");
  for (const [name, value] of Object.entries(issue.customFields)) {
    if (NOISE_FIELD_NAMES.includes(name)) continue;
    lines.push(`  - ${name}：${isEmptyValue(value) ? "（空）" : value}`);
  }
  kv("标签", issue.labels.join(" / "));
  kv("模块", issue.components.join(" / "));
  kv("影响的版本", issue.affectedVersions.join(" / "));
  kv("修复的版本", issue.fixVersions.join(" / "));
  kv("创建/更新", `${issue.created || ""} → ${issue.updated || ""}`);
  const estimate = issue.originalEstimateSeconds === null ? "" : String(issue.originalEstimateSeconds);
  kv("原始预估工时(秒)", estimate);
  kv("剩余工时(秒)", issue.remainingEstimateSeconds === null ? "" : String(issue.remainingEstimateSeconds));
  const tt = issue.timeTracking || {};
  if (tt.originalEstimate) lines.push(`  - timeTracking.originalEstimate：${tt.originalEstimate}`);

  lines.push("");
  lines.push(`## 描述（${issue.description.length} 字）`);
  const desc = issue.description.length > maxDesc
    ? issue.description.slice(0, maxDesc) + `\n…（已截断，全文共 ${issue.description.length} 字）`
    : issue.description;
  lines.push(desc || "（描述为空）");

  lines.push("");
  lines.push(`## 附件（${issue.attachments.length} 个）`);
  for (const a of issue.attachments) {
    lines.push(`- ${a.filename} | ${a.size} B | ${a.mimeType} | ${a.created} | ${a.author}`);
  }
  if (!issue.attachments.length) lines.push("- （无附件）");

  lines.push("");
  lines.push(`## 问题链接（${issue.links.length} 条）`);
  for (const l of issue.links) {
    lines.push(`- [${l.label || l.direction}] ${l.key} ${l.summary}（${l.status}）`);
  }
  if (!issue.links.length) lines.push("- （无链接）");

  lines.push("");
  lines.push(`## 子任务（${issue.subtasks.length} 个，含 parent 关联）`);
  for (const s of issue.subtasks) {
    lines.push(`- ${s.key} ${s.summary}（${s.status}，${s.assignee || "未指派"}）`);
  }
  if (!issue.subtasks.length) lines.push("- （无子任务）");

  lines.push("");
  lines.push(`## 评论（共 ${issue.comments.length} 条，显示最近 ${Math.min(maxComments, issue.comments.length)} 条）`);
  for (const c of issue.comments.slice(-maxComments)) {
    const body = c.body.length > maxCommentChars ? c.body.slice(0, maxCommentChars) + "…（截断）" : c.body;
    lines.push(`### ${c.author} @ ${c.created}${c.updated && c.updated !== c.created ? `（编辑于 ${c.updated}）` : ""}`);
    lines.push(body);
    lines.push("");
  }
  if (!issue.comments.length) lines.push("- （无评论）");

  return lines.join("\n");
}

/** 下载需求单附件到本地目录 */
export async function downloadAttachments(keyOrUrl, outDir, options = {}) {
  const fs = await import("node:fs");
  const path = await import("node:path");
  const issue = options.issue || await getIssue(keyOrUrl, options);
  fs.mkdirSync(outDir, { recursive: true });
  const saved = [];
  for (const a of issue.attachments) {
    if (options.only && !options.only.some((kw) => a.filename.includes(kw))) continue;
    const safeName = String(a.filename).replace(/[\\/:*?"<>|]/g, "_");
    const target = path.join(outDir, safeName);
    try {
      const res = await request("GET", a.content, { ...options, raw: true });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const buf = Buffer.from(await res.arrayBuffer());
      fs.writeFileSync(target, buf);
      saved.push({ ...a, path: target, savedSize: buf.length });
    } catch (e) {
      saved.push({ ...a, path: null, error: e.message });
    }
  }
  return saved;
}

/** 写入评论 */
export async function addComment(keyOrUrl, body, options = {}) {
  const key = parseIssueKey(keyOrUrl);
  assertLockedIssueInput(keyOrUrl);
  if (!key) throw new Error(`无法解析 Jira 问题 Key：${keyOrUrl}`);
  if (!body || !String(body).trim()) throw new Error("评论内容为空，已阻止写入");
  return await request("POST", `/rest/api/2/issue/${encodeURIComponent(key)}/comment`, { ...options, body: { body: String(body) } });
}

/** 更新既有评论 */
export async function updateComment(keyOrUrl, commentId, body, options = {}) {
  const key = parseIssueKey(keyOrUrl);
  assertLockedIssueInput(keyOrUrl);
  return await request("PUT", `/rest/api/2/issue/${encodeURIComponent(key)}/comment/${commentId}`, { ...options, body: { body: String(body) } });
}

/** 找出本单中由 AI 预检产生的历史评论（用于复审时避免重复刷屏） */
export async function findPreviousReviews(keyOrUrl, options = {}) {
  const issue = options.issue || await getIssue(keyOrUrl, options);
  // 兼容旧版评论标题「需求准入 AI 预检」与新版「G1 需求准入结论」。
  return issue.comments.filter((c) => c.body.includes("需求准入 AI 预检") || c.body.includes("G1 需求准入结论"));
}

/** 读取 skill 目录或 ~/.codex/jira/config.json，用于覆盖门禁字段名等可选配置 */
export async function loadConfig() {
  const fs = await import("node:fs");
  const path = await import("node:path");
  const dir = skillDir();
  const candidates = [];
  if (dir) {
    // 同样统一用正斜杠拼接，保证容器（Linux）下也能命中。
    candidates.push(path.resolve(`${dir}/jira-config.json`));
    candidates.push(path.resolve(`${dir}/../../jira/config.json`));
  }
  for (const file of candidates) {
    try {
      if (!fs.existsSync(file)) continue;
      const cfg = JSON.parse(fs.readFileSync(file, "utf8"));
      if (cfg && typeof cfg === "object") return { config: cfg, source: file };
    } catch (e) { /* 忽略单个候选文件的读取错误 */ }
  }
  return { config: {}, source: null };
}

/** 读取该单当前可用的工作流流转（只读），返回 [{ id, name, to }] */
export async function listTransitions(keyOrUrl, options = {}) {
  const key = parseIssueKey(keyOrUrl);
  assertLockedIssueInput(keyOrUrl);
  if (!key) throw new Error(`无法从输入中解析 Jira 问题 Key：${keyOrUrl}`);
  const data = await request("GET", `/rest/api/2/issue/${encodeURIComponent(key)}/transitions`, options);
  const list = (data && Array.isArray(data.transitions)) ? data.transitions : [];
  return list.map((item) => ({
    id: item.id,
    name: item.name,
    to: item.to ? item.to.name : "",
  }));
}

/**
 * 解析「流转人」：默认取经办人（assignee），可在 jira-config.json 用 flowOwnerField 指定其他人员字段。
 * 返回 { name, displayName, mention, field, source }；mention 为 Jira wiki 的 @ 写法，可直接拼进评论。
 * 人员字段全部为空时返回 mention 为空串，由调用方决定是否省略 @。
 */
export async function resolveFlowOwner(issue, options = {}) {
  const config = options.config || (await loadConfig()).config || {};
  const preferred = String(config.flowOwnerField || "assignee").trim();
  const order = [];
  for (const name of [preferred, "assignee", "reporter", "creator"]) {
    if (name && !order.includes(name)) order.push(name);
  }
  for (const field of order) {
    const user = issue ? issue[field] : null;
    if (user && user.name) {
      return {
        name: user.name,
        displayName: user.displayName || user.name,
        // Jira Server / Data Center 的 @ 人语法：按登录名引用，显示为用户全名。
        mention: `[~${user.name}]`,
        field,
        source: field === preferred ? "配置或默认" : `回退到 ${field}`,
      };
    }
  }
  return { name: "", displayName: "", mention: "", field: "", source: null };
}

/**
 * 把需求单流转到指定目标状态（例如「需求设计」）。
 * 已处于目标状态时直接跳过；找不到对应流转时返回可读原因而不是抛错，便于在未配置该流转的项目上优雅降级。
 */
export async function transitionToStatus(keyOrUrl, targetStatus, options = {}) {
  const key = parseIssueKey(keyOrUrl);
  assertLockedIssueInput(keyOrUrl);
  if (!key) throw new Error(`无法从输入中解析 Jira 问题 Key：${keyOrUrl}`);
  const target = String(targetStatus || "").trim();
  if (!target) throw new Error("缺少目标状态名");
  const issue = options.issue || await getIssue(keyOrUrl, options);
  if (issue.status === target) {
    return { ok: true, skipped: true, reason: "already-in-target", status: issue.status, target };
  }
  const available = await listTransitions(keyOrUrl, options);
  // 按目标状态名匹配，而不是写死流转 id：不同项目/工作流的流转 id 与流转名都可能不同。
  const match = available.find((item) => item.to === target) || available.find((item) => item.name === target);
  if (!match) {
    return { ok: false, reason: "no-transition", status: issue.status, target, available };
  }
  await request("POST", `/rest/api/2/issue/${encodeURIComponent(key)}/transitions`, {
    ...options,
    body: { transition: { id: match.id } },
  });
  return { ok: true, skipped: false, from: issue.status, to: target, transition: match.name, transitionId: match.id };
}

/**
 * 把本地文件作为附件上传到需求单（POST /rest/api/2/issue/{KEY}/attachments）。
 * name 默认取本地文件名，可用 options.name 指定 Jira 里的附件名（例如评审报告的规范文件名）。
 * 本地文件缺失、权限不足（403）、该单未启用附件（404）时返回 { ok:false, reason } 而不抛错，便于优雅降级。
 */
export async function uploadAttachment(keyOrUrl, filePath, options = {}) {
  const key = parseIssueKey(keyOrUrl);
  assertLockedIssueInput(keyOrUrl);
  if (!key) throw new Error(`无法从输入中解析 Jira 问题 Key：${keyOrUrl}`);
  const fs = await import("node:fs");
  const path = await import("node:path");
  const absolute = filePath ? path.resolve(String(filePath)) : "";
  if (!absolute || !fs.existsSync(absolute) || !fs.statSync(absolute).isFile()) {
    return {
      ok: false,
      reason: "file-not-found",
      file: String(filePath || ""),
      hint: "本地文件不存在或不是普通文件，先确认报告已生成再上传",
    };
  }
  const name = String(options.name || "").trim() || path.basename(absolute);
  const form = new FormData();
  // 附件内容以二进制读入，避免大文件被编码成字符串。
  form.append("file", new Blob([fs.readFileSync(absolute)]), name);
  try {
    const data = await request("POST", `/rest/api/2/issue/${encodeURIComponent(key)}/attachments`, {
      ...options,
      body: form,
    });
    const first = Array.isArray(data) ? data[0] : data;
    return {
      ok: true,
      id: first && first.id,
      filename: (first && first.filename) || name,
      size: first && first.size,
      content: (first && first.content) || "",
    };
  } catch (err) {
    const status = err && err.status;
    // 403 通常是缺 CREATE_ATTACHMENTS 权限，404 通常是该单未启用附件或 Key 不存在。
    const reason = status === 403 ? "forbidden" : status === 404 ? "not-found" : "http-error";
    return { ok: false, reason, status: status || 0, name, message: String((err && err.message) || err) };
  }
}

/** 定位门禁字段：按候选名精确匹配，返回 { fields, missing, configSource } */
export async function resolveGateFields(options = {}) {
  const { config, source } = await loadConfig();
  const wanted = {
    result: config.gateResultField || DEFAULT_GATE_FIELD_NAMES.result,
    difficulty: config.gateDifficultyField || DEFAULT_GATE_FIELD_NAMES.difficulty,
    checkedAt: config.gateCheckedAtField || DEFAULT_GATE_FIELD_NAMES.checkedAt,
  };
  const all = await request("GET", "/rest/api/2/field", options);
  const byName = new Map();
  for (const f of all) byName.set(String(f.name || "").trim(), f);
  const fields = {};
  const missing = [];
  for (const slot of Object.keys(wanted)) {
    const names = [].concat(wanted[slot] || []);
    fields[slot] = names.map((n) => byName.get(n)).find(Boolean) || null;
    if (!fields[slot]) missing.push(slot + "（候选名：" + names.join(" / ") + "）");
  }
  return { fields, missing, configSource: source, totalFields: all.length };
}

/**
 * 把 AI 预检结论回写为 Jira 字段值，供工作流校验器 / 条件读取。
 * 字段不存在时返回 { skipped: true } 而不抛异常，便于在未配置门禁字段时优雅降级。
 * payload: { result: "通过"|"不通过"|"未检查"|"豁免", difficulty: "高"|"中"|"低", checkedAt: ISO 时间 }
 */
export async function setGateResult(keyOrUrl, payload = {}, options = {}) {
  const key = parseIssueKey(keyOrUrl);
  assertLockedIssueInput(keyOrUrl);
  if (!key) throw new Error(`无法解析 Jira 问题 Key：${keyOrUrl}`);
  const { dryRun, ...rest } = options;
  const { fields, missing } = await resolveGateFields(rest);
  if (!fields.result) {
    return {
      ok: false,
      skipped: true,
      reason: `未找到门禁字段：${missing.join("；")}`,
      hint: "请由 Jira 管理员创建该字段，或在 config.json 中用 gateResultField 指定实际字段名",
    };
  }
  const isOption = (f) => !!f && !!f.schema && (f.schema.type === "option" || f.schema.type === "radiobuttons");
  const body = {};
  const put = (field, value) => {
    if (!field || value === undefined || value === null || value === "") return;
    body[field.id] = isOption(field) ? { value: String(value) } : String(value);
  };
  put(fields.result, payload.result);
  put(fields.difficulty, payload.difficulty);
  if (fields.checkedAt) {
    const iso = payload.checkedAt || new Date().toISOString();
    const dateOnly = fields.checkedAt.schema && fields.checkedAt.schema.type === "date";
    put(fields.checkedAt, dateOnly ? iso.slice(0, 10) : iso);
  }
  if (!Object.keys(body).length) return { ok: false, skipped: true, reason: "没有可写入的门禁字段值" };
  if (dryRun) return { ok: true, dryRun: true, key, wouldWrite: body };
  await request("PUT", `/rest/api/2/issue/${encodeURIComponent(key)}`, { ...rest, body: { fields: body } });
  return { ok: true, key, written: body, missing };
}

/** 读取当前门禁字段值，用于复审与写入后验证 */
export async function getGateResult(keyOrUrl, options = {}) {
  const key = parseIssueKey(keyOrUrl);
  assertLockedIssueInput(keyOrUrl);
  if (!key) throw new Error(`无法解析 Jira 问题 Key：${keyOrUrl}`);
  const { fields, missing } = await resolveGateFields(options);
  if (!fields.result) return { ok: false, skipped: true, reason: `未找到门禁字段：${missing.join("；")}` };
  const ids = [fields.result, fields.difficulty, fields.checkedAt].filter(Boolean).map((f) => f.id);
  const data = await request("GET", `/rest/api/2/issue/${encodeURIComponent(key)}?fields=${ids.join(",")}`, options);
  const read = (f) => {
    if (!f) return null;
    const v = data && data.fields ? data.fields[f.id] : null;
    if (v === null || v === undefined) return null;
    if (typeof v === "object") return v.value || v.name || JSON.stringify(v);
    return v;
  };
  return { ok: true, key, result: read(fields.result), difficulty: read(fields.difficulty), checkedAt: read(fields.checkedAt) };
}

/** 自检：确认令牌、网络、账号与字段可见性 */
export async function selftest(options = {}) {
  const result = { ok: false, steps: [] };
  const cred = options.token ? { token: options.token, source: "argument" } : await resolveToken(null);
  result.tokenSource = cred.source || null;
  result.checkedPaths = cred.checked || null;
  result.baseUrl = envLocked()
    ? (readEnv("JIRA_BASE_URL") || DEFAULT_BASE_URL)
    : (options.baseUrl || cred.baseUrl || readEnv("JIRA_BASE_URL") || DEFAULT_BASE_URL);
  // 显式标注地址来源：避免在容器内静默连到生产 Jira 而无法察觉。
  if (envLocked()) result.baseUrlSource = "环境变量 JIRA_BASE_URL（服务端锁定）";
  else if (options.baseUrl) result.baseUrlSource = "调用参数";
  else if (readEnv("JIRA_BASE_URL")) result.baseUrlSource = "环境变量 JIRA_BASE_URL";
  else if (cred.baseUrl) result.baseUrlSource = "令牌文件";
  else result.baseUrlSource = "内置默认值（生产 Jira）";
  result.auth = String(cred.auth || "bearer").toLowerCase();
  if (!cred.token) {
    result.steps.push({ step: "读取令牌", ok: false, detail: "未找到令牌文件；请把令牌放到 skill 目录、~/.codex/jira-token.txt 或 ~/.codex/jira/config.json" });
    return result;
  }
  result.steps.push({ step: "读取令牌", ok: true, detail: cred.source });
  try {
    const info = await request("GET", "/rest/api/2/serverInfo", { baseUrl: result.baseUrl });
    result.steps.push({ step: "访问 Jira", ok: true, detail: `${info.baseUrl} v${info.version}` });
  } catch (e) {
    result.steps.push({ step: "访问 Jira", ok: false, detail: e.message });
    return result;
  }
  try {
    const me = await myself({ baseUrl: result.baseUrl });
    result.steps.push({ step: "校验令牌", ok: true, detail: `${me.displayName}（${me.name}）` });
    result.user = me.name;
  } catch (e) {
    result.steps.push({ step: "校验令牌", ok: false, detail: e.message });
    return result;
  }
  result.ok = true;
  return result;
}
