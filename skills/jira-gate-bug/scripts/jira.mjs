/**
 * jira.mjs —— 缺陷单门禁（缺陷单完整性检查 + 自查结果检查 + 故障分析回写）使用的 Jira 客户端。
 *
 * 运行环境约束：
 *   - 服务器容器 / codex exec：没有 node_repl，直接通过 shell 执行 scripts/jira-cli.mjs。
 *   - 桌面 Codex（node_repl）：没有 process、不支持静态 import，因此本文件只用 await import()
 *     动态加载 node 内置模块，令牌从文件读取；改脚本后请用 ?v=时间戳 强制重新加载。
 *   - 所有写操作（评论、附件、门禁字段、流转）都走 REST，请求头固定带 X-Atlassian-Token: no-check。
 */
export const DEFAULT_BASE_URL = "https://jira.in.wezhuiyi.com";

/** 令牌文件候选名（相对技能目录、技能目录上一级与 ~/.codex） */
const TOKEN_FILE_NAMES = [".jira-token", ".jira-token.txt", "jira-token.txt"];

/** 缺陷单门禁字段默认候选名，可用 jira-config.json 覆盖 */
const DEFAULT_GATE_FIELD_NAMES = {
  result: ["缺陷单预检结论", "缺陷单门禁结论", "缺陷单准入结论"],
  selfCheck: ["缺陷单自查结论", "缺陷单自查结果"],
  report: ["缺陷单分析报告", "故障分析报告"],
  checkedAt: ["缺陷单预检时间", "缺陷单门禁时间", "缺陷单准入预检时间"],
};

/** 自查结果评论的判定关键词，可用 jira-config.json 的 selfCheckKeywords 覆盖 */
export const SELF_CHECK_KEYWORDS = {
  headline: ["自查", "自检", "自我排查", "自测"],
  conclusion: ["结论", "结果", "通过", "正常", "无异常", "已定位", "已确认", "已修复", "根因", "原因"],
  evidence: ["日志", "截图", "复现", "环境", "版本", "时间点", "步骤", "附件", "操作", "报错"],
};

/** 效果类缺陷跳过日志/复现要求的固定评论语句。 */
export const LOG_REQUIREMENT_EXEMPTION_PHRASE = "明确缺陷不需要额外提供日志信息";

/** Jira 中可能承载「问题发现产品」的字段名，按优先级排列。 */
const PRODUCT_FIELD_NAMES = ["问题发现产品", "发现产品", "产品归属"];

/** 故障分析评论固定块的字段别名。 */
const FAULT_SCOPE_KEY_ALIASES = {
  product: ["product", "productname", "产品", "产品名", "问题发现产品"],
  module: ["module", "modulename", "模块", "模块名"],
  version: ["version", "tag", "tagversion", "版本", "标签", "标签版本"],
  branch: ["branch", "branchaddress", "repo", "repourl", "repository", "分支", "仓库", "仓库地址"],
};

/** 从任意输入中解析 Jira 问题 Key */
export function parseIssueKey(input) {
  if (!input) return null;
  const raw = String(input).trim();
  let decoded = raw;
  try { decoded = decodeURIComponent(raw); } catch (e) { decoded = raw; }
  const haystack = decoded === raw ? raw : raw + "\n" + decoded;
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

/** 返回技能目录的绝对路径（统一用正斜杠，失败时返回 null） */
export function skillDir() {
  try {
    const m = String(import.meta.url).match(/^file:\/\/\/(.+)\/scripts\/jira\.mjs(?:[?#].*)?$/i);
    if (!m) return null;
    let dir = m[1];
    try { dir = decodeURIComponent(dir); } catch (e) { /* 保留原始路径 */ }
    return dir.replace(/\\/g, "/").replace(/\/+$/, "");
  } catch (e) {
    return null;
  }
}

/** 读取 JIRA_TOKEN 环境变量（node_repl 下没有 process） */
function envToken() {
  try {
    if (typeof process !== "undefined" && process.env && process.env.JIRA_TOKEN) {
      return String(process.env.JIRA_TOKEN).trim() || null;
    }
  } catch (e) { /* node_repl 无 process */ }
  return null;
}

/** 服务端锁定标记：为 "1" 时技能只认环境变量，禁止回落到令牌文件与命令行入参 */
export const ENV_LOCK_FLAG = "SKILLRUN_ENV_LOCKED";

/** 是否处于服务端锁定模式（容器内由后端注入 SKILLRUN_ENV_LOCKED=1） */
export function envLocked() {
  return envValue(ENV_LOCK_FLAG) === "1";
}

/** 去掉末尾斜杠并转小写，用于判断两个地址是否同一个站点 */
function normalizeUrlForCompare(value) {
  return String(value || "").trim().replace(/\/+$/, "").toLowerCase();
}

/** 锁定模式下的权威地址：未注入时直接报错，绝不回落到内置默认站点 */
function lockedBaseUrl() {
  const value = envValue("JIRA_BASE_URL");
  if (!value) {
    throw new Error("运行环境已锁定，但服务端未注入 JIRA_BASE_URL；请检查 .env 配置后重启 api/worker。");
  }
  return String(value).replace(/\/+$/, "");
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

/** 读取环境变量（node_repl 下缺失时返回空串） */
export function envValue(name) {
  try {
    if (typeof process !== "undefined" && process.env && process.env[name]) {
      return String(process.env[name]);
    }
  } catch (e) { /* node_repl 无 process */ }
  return "";
}

/**
 * 解析 Jira 令牌，返回 { token, source, baseUrl, auth, username }。
 * 优先级：显式传入 > JIRA_TOKEN 环境变量 > 技能目录与 ~/.codex 下的令牌文件。
 */
export async function resolveToken(explicit) {
  if (envLocked()) {
    // 锁定模式：令牌只认服务端注入的 JIRA_TOKEN，不读令牌文件、不认命令行入参。
    const lockedToken = envToken();
    const explicitToken = explicit ? String(explicit).trim() : "";
    if (explicitToken && explicitToken !== lockedToken) {
      throw new Error("运行环境已锁定：不允许用 --token 覆盖服务端注入的 JIRA_TOKEN。");
    }
    return {
      token: lockedToken || null,
      source: lockedToken ? "环境变量 JIRA_TOKEN（服务端锁定）" : null,
      baseUrl: envValue("JIRA_BASE_URL") || null,
    };
  }
  const fromEnv = String(explicit || "").trim() || envToken();
  if (fromEnv) return { token: fromEnv, source: explicit ? "argument" : "environment" };

  const fs = await import("node:fs");
  const dir = skillDir();
  const candidates = [];
  if (dir) {
    for (const name of TOKEN_FILE_NAMES) candidates.push(dir + "/" + name);
    for (const name of TOKEN_FILE_NAMES) candidates.push(dir + "/../" + name);
    candidates.push(dir + "/../../jira-token.txt");
    candidates.push(dir + "/../../jira/token.txt");
    candidates.push(dir + "/../../jira/config.json");
  }
  for (const file of candidates) {
    try {
      if (!fs.existsSync(file)) continue;
      const text = fs.readFileSync(file, "utf8").trim();
      if (!text) continue;
      if (text.startsWith("{")) {
        const cfg = JSON.parse(text);
        if (cfg.token) {
          return {
            token: String(cfg.token).trim(),
            source: file,
            baseUrl: cfg.baseUrl,
            auth: cfg.auth,
            username: cfg.username,
          };
        }
        continue;
      }
      const firstLine = text.split(/\r?\n/).find((line) => line.trim()) || "";
      const token = firstLine.replace(/^JIRA_PAT\s*=\s*/i, "").trim();
      if (token) return { token, source: file };
    } catch (e) { /* 忽略单个候选文件的读取错误 */ }
  }
  return { token: null, source: null, checked: candidates };
}

/** 组装认证头：默认 Bearer；配置 auth=basic 时用 Basic */
export function buildAuthHeader(cred) {
  const mode = String(cred.auth || "bearer").toLowerCase();
  if (mode === "basic") {
    const raw = String(cred.username || "") + ":" + String(cred.token || "");
    if (typeof Buffer !== "undefined") return "Basic " + Buffer.from(raw, "utf8").toString("base64");
    return "Basic " + btoa(unescape(encodeURIComponent(raw)));
  }
  return "Bearer " + cred.token;
}

/** 统一发起 REST 请求：options 支持 token/baseUrl/body/formData/raw/skipAuth */
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
    baseUrl = String(options.baseUrl || cred.baseUrl || DEFAULT_BASE_URL).replace(/\/+$/, "");
  }
  const headers = { Accept: "application/json", "X-Atlassian-Token": "no-check" };
  if (cred.token) headers.Authorization = buildAuthHeader(cred);
  let payload;
  if (options.formData !== undefined) {
    payload = options.formData;
  } else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = typeof options.body === "string" ? options.body : JSON.stringify(options.body);
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
    const err = new Error("Jira " + method + " " + apiPath + " 失败：HTTP " + res.status + (detail ? " — " + detail : ""));
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

/** 与判定无关的纯噪音字段，渲染时跳过 */
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

/** 清理 Jira wiki 渲染标记，避免噪音干扰判定 */
export function cleanWikiMarkup(text) {
  if (!text) return "";
  return String(text)
    .replace(/\{\s*color(:[^}]*)?\s*\}/gi, "")
    .replace(/\{\s*(?:code|noformat|panel|quote|info|note|warning|tip|expand)(?::[^}]*)?\s*\}/gi, "")
    .replace(/\[\^([^\]]+)\]/g, "$1")
    .replace(/\{anchor:[^}]*\}/gi, "")
    .trim();
}

/** 是否视为“没有填写” */
export function isEmptyValue(value) {
  const text = toPlainText(value).trim();
  return text === "" || NO_VALUE.includes(text);
}


/** 返回去重后的非空文本列表，兼容数组与 Jira 常见的多值返回。 */
function uniqueTextValues(value) {
  const source = Array.isArray(value) ? value : [value];
  const result = [];
  for (const item of source) {
    const text = toPlainText(item).trim();
    if (!text || isEmptyValue(text) || result.includes(text)) continue;
    result.push(text);
  }
  return result;
}


/** 解析评论中的固定块：[FAULT_ANALYSIS] / [故障分析]。 */
function parseFaultScopeBlock(body) {
  const aliases = Object.entries(FAULT_SCOPE_KEY_ALIASES).reduce((result, [key, names]) => {
    for (const name of names) result[name.toLowerCase().replace(/[\s_-]+/g, "")] = key;
    return result;
  }, {});
  const values = {};
  const entries = [];
  let inBlock = false;
  for (const line of String(body || "").split(/\r?\n/)) {
    const text = line.trim().replace(/^[*-]\s+/, "");
    if (/^\[\s*(?:FAULT_ANALYSIS|故障分析(?:参数)?)\s*\]$/i.test(text)) {
      inBlock = true;
      continue;
    }
    if (!inBlock) continue;
    if (!text) continue;
    if (/^\[.+\]$/.test(text)) break;
    const bareModule = text.match(/^([A-Za-z0-9_.-]+)$/);
    if (bareModule) {
      entries.push({ module: bareModule[1], tag: "" });
      continue;
    }
    const match = text.match(/^([^:=]{1,40})\s*[:=]\s*(.+)$/);
    if (!match) continue;
    const normalizedKey = match[1].trim().toLowerCase().replace(/[\s_-]+/g, "");
    const key = aliases[normalizedKey];
    const value = match[2].trim();
    if (key) {
      if (value) values[key] = value;
      continue;
    }
    const pair = text.match(/^([A-Za-z0-9_.-]+)\s*:\s*(\S+)$/);
    if (pair) entries.push({ module: pair[1], tag: pair[2] });
  }
  return (Object.keys(values).length || entries.length) ? { values, entries } : null;
}


/** 从最新评论向前寻找故障分析固定块，返回解析值及来源评论。 */
export function extractFaultAnalysisBlock(issue) {
  const comments = (issue && issue.comments) || [];
  for (let index = comments.length - 1; index >= 0; index -= 1) {
    const comment = comments[index];
    const parsed = parseFaultScopeBlock(comment && comment.body);
    if (!parsed) continue;
    return {
      found: true,
      values: parsed.values,
      entries: parsed.entries,
      commentId: comment.id,
      author: comment.author || "",
      created: comment.created || "",
    };
  }
  return { found: false, values: {} };
}


/** 提取环境文本中的明确版本号，供 Jira 版本字段缺失时兜底。 */
function versionsFromEnvironment(value) {
  const labelled = String(value || "")
    .split(/\r?\n/)
    .filter((line) => /(?:版本|version|tag|release)/i.test(line))
    .join(" ");
  if (!labelled) return [];
  const matches = labelled.match(/\bv?\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9._-]+)*\b/gi) || [];
  return uniqueTextValues(matches);
}


/**
 * 从一张 Jira 缺陷单中提取故障分析定位信息。
 *
 * 产品：调用方覆盖 > Jira「问题发现产品」 > 评论固定块（兼容人工纠正）。
 * 模块：调用方覆盖 > 评论固定块 > Jira 组件（唯一值才自动采用）。
 * 版本：调用方覆盖 > 评论固定块 > 影响版本/修复版本/环境（多来源只接受唯一值）。
 * 分支：调用方覆盖 > 评论固定块；仓库地址与分支规则仍由 scope-mapping.json 维护。
 */
export function extractFaultAnalysisScope(issue, overrides = {}) {
  const block = extractFaultAnalysisBlock(issue);
  const sources = {};
  const candidates = { module: [], version: [] };
  const relatedModules = [];
  const pick = (...values) => values.map((item) => String(item || "").trim()).find((item) => item && !isEmptyValue(item)) || "";
  const pairEntries = Array.isArray(block.entries) ? block.entries : [];
  const primaryPair = pairEntries[0] || null;

  const customFields = (issue && issue.customFields) || {};
  let jiraProduct = "";
  let jiraProductField = "";
  for (const name of PRODUCT_FIELD_NAMES) {
    if (!isEmptyValue(customFields[name])) {
      jiraProduct = String(customFields[name]).trim();
      jiraProductField = name;
      break;
    }
  }

  const product = pick(overrides.product, jiraProduct, block.values.product);
  if (product) {
    sources.product = overrides.product && product === String(overrides.product).trim()
      ? "调用方输入"
      : jiraProduct && product === jiraProduct
        ? `Jira:${jiraProductField}`
        : `评论:${block.commentId || ""}`;
  }

  const moduleCandidates = uniqueTextValues(issue && issue.components);
  const module = pick(overrides.module, block.values.module, primaryPair && primaryPair.module);
  if (module) {
    sources.module = overrides.module && module === String(overrides.module).trim()
      ? "调用方输入"
      : block.values.module && module === block.values.module
        ? `评论:${block.commentId || ""}`
        : `评论:${block.commentId || ""}:${module}`;
  } else if (moduleCandidates.length === 1) {
    sources.module = "Jira:模块/组件";
  } else if (moduleCandidates.length > 1) {
    candidates.module = moduleCandidates;
  }
  const moduleValue = module || (moduleCandidates.length === 1 ? moduleCandidates[0] : "");

  const versionCandidates = [];
  const addVersionSource = (source, values) => {
    for (const value of uniqueTextValues(values)) versionCandidates.push({ source, value });
  };
  addVersionSource("Jira:影响版本", issue && issue.affectedVersions);
  addVersionSource("Jira:修复版本", issue && issue.fixVersions);
  addVersionSource("Jira:环境", versionsFromEnvironment(issue && issue.environment));
  const uniqueVersions = [...new Set(versionCandidates.map((item) => item.value))];
  let version = pick(overrides.version, block.values.version, primaryPair && primaryPair.tag);
  if (version) {
    sources.version = overrides.version && version === String(overrides.version).trim()
      ? "调用方输入"
      : block.values.version && version === block.values.version
        ? `评论:${block.commentId || ""}`
        : `评论:${block.commentId || ""}:${version}`;
  } else if (uniqueVersions.length === 1) {
    version = uniqueVersions[0];
    sources.version = versionCandidates.find((item) => item.value === version).source;
  } else {
    candidates.version = uniqueVersions;
  }

  const branch = pick(overrides.branch, block.values.branch);
  if (branch) sources.branch = overrides.branch && branch === String(overrides.branch).trim() ? "调用方输入" : `评论:${block.commentId || ""}`;

  for (const entry of pairEntries) {
    if (entry.module === module && (!version || entry.tag === version)) continue;
    relatedModules.push({ module: entry.module, version: entry.tag, source: `评论:${block.commentId || ""}` });
  }

  const missing = [];
  if (!product) missing.push("product");
  if (!moduleValue) missing.push("module");
  if (!version) missing.push("version");

  return {
    product,
    module: moduleValue,
    version,
    branch,
    relatedModules,
    sources,
    candidates,
    missing,
    block,
  };
}

/** 把 Jira 原始返回规范化成便于判定的结构 */
export function normalizeIssue(raw) {
  const f = raw.fields || {};
  const names = raw.names || {};
  const baseUrl = (raw.self || "").split("/rest/")[0] || DEFAULT_BASE_URL;

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

  const descriptionHtml = (raw.renderedFields && raw.renderedFields.description) || "";
  const description = cleanWikiMarkup(toPlainText(f.description) || htmlToText(descriptionHtml));

  return {
    key: raw.key,
    id: raw.id,
    url: baseUrl + "/browse/" + raw.key,
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
    environment: toPlainText(f.environment),
    description,
    descriptionHtml,
    customFields,
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
      body: cleanWikiMarkup(toPlainText(c.body)),
    })),
  };
}

/** 通过 JQL 查询以该单为父的子任务（部分项目 subtasks 字段不可用时兜底） */
export async function fetchSubtasksByParent(keyOrUrl, options = {}) {
  const key = parseIssueKey(keyOrUrl);
  assertLockedIssueInput(keyOrUrl);
  if (!key) return [];
  try {
    const jql = encodeURIComponent("parent = " + key);
    const data = await request("GET", "/rest/api/2/search?jql=" + jql + "&fields=summary,status,assignee,issuetype&maxResults=50", options);
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

/** 获取并规范化单个缺陷单（含全量评论与父单兜底子任务） */
export async function getIssue(keyOrUrl, options = {}) {
  const key = parseIssueKey(keyOrUrl);
  assertLockedIssueInput(keyOrUrl);
  if (!key) throw new Error("无法从输入中解析 Jira 问题 Key：" + keyOrUrl);
  const data = await request("GET", "/rest/api/2/issue/" + encodeURIComponent(key) + "?expand=names,renderedFields,schema", options);
  const issue = normalizeIssue(data);
  if (!issue.subtasks.length) {
    const found = await fetchSubtasksByParent(key, options);
    if (found.length) issue.subtasks = found;
  }
  const commentMeta = (data.fields && data.fields.comment) || {};
  if (commentMeta.total && commentMeta.total > issue.comments.length) {
    const all = await request("GET", "/rest/api/2/issue/" + encodeURIComponent(key) + "/comment", options);
    if (all && Array.isArray(all.comments)) {
      issue.comments = all.comments.map((c) => ({
        id: c.id,
        author: c.author ? c.author.displayName : "",
        authorName: c.author ? c.author.name : "",
        created: c.created,
        updated: c.updated,
        body: cleanWikiMarkup(toPlainText(c.body)),
      }));
    }
  }
  return issue;
}

/** 把规范化后的缺陷单渲染为便于判定的 Markdown 摘要 */
export function renderIssueMarkdown(issue, options = {}) {
  const maxDesc = options.maxDescriptionChars || 8000;
  const maxComments = options.maxComments || 10;
  const maxCommentChars = options.maxCommentChars || 1200;
  const lines = [];
  const kv = (k, v) => lines.push("- " + k + "：" + (isEmptyValue(v) ? "（空）" : toPlainText(v)));

  lines.push("# " + issue.key + " " + (issue.summary || ""));
  lines.push("");
  lines.push("- 链接：" + issue.url);
  kv("类型", issue.issueType);
  kv("状态", issue.status);
  kv("优先级", issue.priority);
  kv("经办人", issue.assignee ? issue.assignee.displayName : "");
  kv("报告人", issue.reporter ? issue.reporter.displayName : "");
  kv("模块", (issue.components || []).join("、"));
  kv("影响版本", (issue.affectedVersions || []).join("、"));
  kv("修复版本", (issue.fixVersions || []).join("、"));
  kv("环境", issue.environment);
  kv("创建 / 更新", (issue.created || "") + " / " + (issue.updated || ""));

  const customKeys = Object.keys(issue.customFields || {}).filter((name) => !NOISE_FIELD_NAMES.includes(name));
  if (customKeys.length) {
    lines.push("");
    lines.push("## 自定义字段");
    for (const name of customKeys) kv(name, issue.customFields[name]);
  }

  lines.push("");
  lines.push("## 描述");
  const description = String(issue.description || "").slice(0, maxDesc);
  lines.push(description || "（描述为空）");

  if (issue.attachments.length) {
    lines.push("");
    lines.push("## 附件（" + issue.attachments.length + " 个）");
    for (const a of issue.attachments) {
      lines.push("- " + a.filename + "（" + Math.round((a.size || 0) / 1024) + " KB，" + (a.author || "未知") + "，" + (a.created || "") + "）");
    }
  }

  if (issue.links.length) {
    lines.push("");
    lines.push("## 问题链接");
    for (const l of issue.links) lines.push("- " + l.label + " " + l.key + " " + (l.summary || "") + "（" + (l.status || "") + "）");
  }

  const logExemption = findLogRequirementExemption(issue);
  if (logExemption.found) {
    const source = logExemption.matches[logExemption.matches.length - 1];
    lines.push("");
    lines.push("## 日志/复现豁免");
    lines.push("- 命中固定语句：明确缺陷不需要额外提供日志信息");
    lines.push("- 来源评论：" + (source.author || "未知") + " @ " + (source.created || "") + "（评论 #" + (source.commentId || "") + "）");
    lines.push("- 使用前提：本单必须判定为效果类缺陷；其它缺陷仍按常规检查。");
  }

  lines.push("");
  lines.push("## 评论（共 " + issue.comments.length + " 条，显示最近 " + Math.min(maxComments, issue.comments.length) + " 条）");
  for (const c of issue.comments.slice(-maxComments)) {
    const body = c.body.length > maxCommentChars ? c.body.slice(0, maxCommentChars) + "…（截断）" : c.body;
    lines.push("### " + c.author + " @ " + c.created + (c.updated && c.updated !== c.created ? "（编辑于 " + c.updated + "）" : ""));
    lines.push(body);
    lines.push("");
  }
  if (!issue.comments.length) lines.push("- （无评论）");

  return lines.join("\n");
}

/** 下载缺陷单附件到本地目录，返回 [{ filename, path, size }] */
export async function downloadAttachments(keyOrUrl, outDir, options = {}) {
  const fs = await import("node:fs");
  const issue = options.issue || await getIssue(keyOrUrl, options);
  const saved = [];
  try { fs.mkdirSync(outDir, { recursive: true }); } catch (e) { /* 目录已存在 */ }
  for (const a of issue.attachments) {
    if (options.only && !options.only.some((kw) => String(a.filename).includes(kw))) continue;
    const safeName = String(a.filename).replace(/[\\/:*?"<>|]/g, "_");
    const target = outDir.replace(/\/+$/, "") + "/" + safeName;
    try {
      const res = await request("GET", a.content, { ...options, raw: true });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const buf = Buffer.from(await res.arrayBuffer());
      fs.writeFileSync(target, buf);
      saved.push({ filename: a.filename, path: target, size: buf.length });
    } catch (e) {
      saved.push({ filename: a.filename, path: null, error: e.message });
    }
  }
  return saved;
}

/** 写入评论 */
export async function addComment(keyOrUrl, body, options = {}) {
  const key = parseIssueKey(keyOrUrl);
  assertLockedIssueInput(keyOrUrl);
  if (!key) throw new Error("无法解析 Jira 问题 Key：" + keyOrUrl);
  if (!body || !String(body).trim()) throw new Error("评论内容为空，已阻止写入");
  return await request("POST", "/rest/api/2/issue/" + encodeURIComponent(key) + "/comment", { ...options, body: { body: String(body) } });
}

/** 更新既有评论 */
export async function updateComment(keyOrUrl, commentId, body, options = {}) {
  const key = parseIssueKey(keyOrUrl);
  assertLockedIssueInput(keyOrUrl);
  return await request("PUT", "/rest/api/2/issue/" + encodeURIComponent(key) + "/comment/" + commentId, { ...options, body: { body: String(body) } });
}

/**
 * 上传附件（用于把故障分析 HTML 报告放进缺陷单评论区）。
 * 依赖运行时提供 FormData / Blob（Node 18+ 与 node_repl 均可用）。
 */
export async function addAttachment(keyOrUrl, filePath, options = {}) {
  const fs = await import("node:fs");
  const path = await import("node:path");
  const key = parseIssueKey(keyOrUrl);
  assertLockedIssueInput(keyOrUrl);
  if (!key) throw new Error("无法解析 Jira 问题 Key：" + keyOrUrl);
  if (!fs.existsSync(filePath)) throw new Error("附件文件不存在：" + filePath);
  if (typeof FormData === "undefined" || typeof Blob === "undefined") {
    throw new Error("当前运行时缺少 FormData/Blob，无法上传附件；请改用 Node 18+ 执行 jira-cli.mjs");
  }
  const filename = options.filename || path.basename(filePath);
  const form = new FormData();
  form.append("file", new Blob([fs.readFileSync(filePath)]), filename);
  return await request("POST", "/rest/api/2/issue/" + encodeURIComponent(key) + "/attachments", {
    ...options,
    formData: form,
  });
}

/** 从评论中筛选满足判定的条目，返回带证据片段的数组 */
export function findComments(issue, predicate) {
  const comments = (issue && issue.comments) || [];
  return comments.filter((c) => {
    try { return !!predicate(c); } catch (e) { return false; }
  });
}

/**
 * 查找效果类缺陷的日志/复现豁免声明。
 * 仅精确命中约定语句才返回 found=true；是否属于效果类缺陷仍由门禁结合单内容判断。
 */
export function findLogRequirementExemption(issue) {
  const matches = [];
  for (const comment of (issue && issue.comments) || []) {
    const text = String(comment.body || "");
    if (!text.includes(LOG_REQUIREMENT_EXEMPTION_PHRASE)) continue;
    matches.push({
      commentId: comment.id,
      author: comment.author,
      created: comment.created,
      phrase: LOG_REQUIREMENT_EXEMPTION_PHRASE,
    });
  }
  return {
    found: matches.length > 0,
    matches,
    phrase: LOG_REQUIREMENT_EXEMPTION_PHRASE,
    reason: matches.length
      ? "评论区已明确声明：" + LOG_REQUIREMENT_EXEMPTION_PHRASE
      : "评论区未找到精确语句：" + LOG_REQUIREMENT_EXEMPTION_PHRASE,
  };
}

/**
 * 判断缺陷单评论区里是否已经有“自查结果”。
 * 规则：评论中同时出现自查类关键词、结论类关键词与证据类关键词，才算有效自查结果。
 * 关键词可用 jira-config.json 的 selfCheckKeywords 覆盖（{ headline, conclusion, evidence }）。
 */
export function findSelfCheckComments(issue, options = {}) {
  const keywords = {
    headline: options.headline || SELF_CHECK_KEYWORDS.headline,
    conclusion: options.conclusion || SELF_CHECK_KEYWORDS.conclusion,
    evidence: options.evidence || SELF_CHECK_KEYWORDS.evidence,
  };
  const hit = (text, list) => list.some((kw) => text.includes(kw));
  const matches = [];
  for (const comment of (issue && issue.comments) || []) {
    const text = String(comment.body || "");
    if (!text.trim()) continue;
    const headline = hit(text, keywords.headline);
    const conclusion = hit(text, keywords.conclusion);
    const evidence = hit(text, keywords.evidence);
    if (!(headline && conclusion && evidence)) continue;
    const line = text.split(/\r?\n/).find((item) => item.trim()) || text.slice(0, 120);
    matches.push({
      commentId: comment.id,
      author: comment.author,
      created: comment.created,
      snippet: line.trim().slice(0, 200),
    });
  }
  return {
    found: matches.length > 0,
    matches,
    reason: matches.length
      ? "评论区存在含自查结论与证据的评论：" + matches[0].snippet
      : "评论区未找到同时包含自查动作、结论与证据的评论",
  };
}

/** 列出当前单可用的流转（用于可选的“打回”流转） */
export async function listTransitions(keyOrUrl, options = {}) {
  const key = parseIssueKey(keyOrUrl);
  assertLockedIssueInput(keyOrUrl);
  if (!key) throw new Error("无法解析 Jira 问题 Key：" + keyOrUrl);
  const data = await request("GET", "/rest/api/2/issue/" + encodeURIComponent(key) + "/transitions", options);
  return ((data && data.transitions) || []).map((t) => ({ id: t.id, name: t.name, to: t.to ? t.to.name : "" }));
}

/**
 * 执行一次流转，transition 可传流转名或 id。
 * 默认不调用：只有 jira-config.json 显式配置了打回流转名时，技能才会调用本函数。
 */
export async function transitionIssue(keyOrUrl, transition, options = {}) {
  const key = parseIssueKey(keyOrUrl);
  assertLockedIssueInput(keyOrUrl);
  if (!key) throw new Error("无法解析 Jira 问题 Key：" + keyOrUrl);
  const wanted = String(transition || "").trim();
  if (!wanted) throw new Error("缺少流转名称或 id");
  const transitions = await listTransitions(key, options);
  const target = transitions.find((t) => t.name === wanted || t.id === wanted || t.to === wanted);
  if (!target) {
    throw new Error("未找到流转：" + wanted + "；当前可用流转：" + transitions.map((t) => t.name).join("、"));
  }
  await request("POST", "/rest/api/2/issue/" + encodeURIComponent(key) + "/transitions", {
    ...options,
    body: { transition: { id: target.id } },
  });
  return { ok: true, key, transition: target };
}

/** 读取技能目录或 ~/.codex/jira/config.json，用于覆盖门禁字段名与打回流转 */
export async function loadConfig() {
  const fs = await import("node:fs");
  const dir = skillDir();
  const candidates = [];
  if (dir) {
    candidates.push(dir + "/jira-config.json");
    candidates.push(dir + "/../../jira/config.json");
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

/** 从配置或默认候选中得到某个字段位的字段名列表 */
function wantedNames(config, keys, fallback) {
  for (const key of keys) {
    if (config && config[key]) return [].concat(config[key]);
  }
  return fallback;
}

/** 定位门禁字段：返回 { fields, missing, configSource, totalFields } */
export async function resolveGateFields(options = {}) {
  const { config, source } = await loadConfig();
  const wanted = {
    result: wantedNames(config, ["gateResultField", "defectGateResultField"], DEFAULT_GATE_FIELD_NAMES.result),
    selfCheck: wantedNames(config, ["gateSelfCheckField", "defectGateSelfCheckField"], DEFAULT_GATE_FIELD_NAMES.selfCheck),
    report: wantedNames(config, ["gateReportField", "defectGateReportField"], DEFAULT_GATE_FIELD_NAMES.report),
    checkedAt: wantedNames(config, ["gateCheckedAtField", "defectGateCheckedAtField"], DEFAULT_GATE_FIELD_NAMES.checkedAt),
  };
  const all = await request("GET", "/rest/api/2/field", options);
  const byName = new Map();
  for (const f of all) byName.set(String(f.name || "").trim(), f);
  const fields = {};
  const missing = [];
  for (const slot of Object.keys(wanted)) {
    const names = [].concat(wanted[slot] || []);
    const found = names.map((name) => byName.get(String(name).trim())).find(Boolean);
    if (found) fields[slot] = found;
    else missing.push(names.join(" / "));
  }
  return { fields, missing, configSource: source, totalFields: all.length };
}

/**
 * 把缺陷单预检结论回写为 Jira 字段，供工作流条件/校验器读取。
 * payload: { result: "通过"|"不通过"|"未检查", selfCheck: "有"|"无", report: "文件名或链接", checkedAt: ISO }
 * 字段不存在时返回 { skipped: true } 而不抛异常，便于优雅降级。
 */
export async function setGateResult(keyOrUrl, payload = {}, options = {}) {
  const key = parseIssueKey(keyOrUrl);
  assertLockedIssueInput(keyOrUrl);
  if (!key) throw new Error("无法解析 Jira 问题 Key：" + keyOrUrl);
  const { dryRun, ...rest } = options;
  const { fields, missing } = await resolveGateFields(rest);
  if (!fields.result) {
    return {
      ok: false,
      skipped: true,
      reason: "未找到门禁字段：" + missing.join("；"),
      hint: "请由 Jira 管理员创建该字段，或在 jira-config.json 中用 gateResultField 指定实际字段名",
    };
  }
  const isOption = (f) => !!f && !!f.schema && (f.schema.type === "option" || f.schema.type === "radiobuttons");
  const body = {};
  const put = (field, value) => {
    if (!field || value === undefined || value === null || value === "") return;
    body[field.id] = isOption(field) ? { value: String(value) } : String(value);
  };
  put(fields.result, payload.result);
  put(fields.selfCheck, payload.selfCheck);
  put(fields.report, payload.report);
  if (fields.checkedAt) {
    const iso = payload.checkedAt || new Date().toISOString();
    const dateOnly = fields.checkedAt.schema && fields.checkedAt.schema.type === "date";
    put(fields.checkedAt, dateOnly ? iso.slice(0, 10) : iso);
  }
  if (!Object.keys(body).length) return { ok: false, skipped: true, reason: "没有可写入的门禁字段值" };
  if (dryRun) return { ok: true, dryRun: true, key, wouldWrite: body };
  await request("PUT", "/rest/api/2/issue/" + encodeURIComponent(key), { ...rest, body: { fields: body } });
  return { ok: true, key, written: body, missing };
}

/** 读取当前门禁字段值，用于写入后验证 */
export async function getGateResult(keyOrUrl, options = {}) {
  const key = parseIssueKey(keyOrUrl);
  assertLockedIssueInput(keyOrUrl);
  if (!key) throw new Error("无法解析 Jira 问题 Key：" + keyOrUrl);
  const { fields, missing } = await resolveGateFields(options);
  if (!fields.result) return { ok: false, skipped: true, reason: "未找到门禁字段：" + missing.join("；") };
  const ids = ["result", "selfCheck", "report", "checkedAt"].filter((slot) => fields[slot]).map((slot) => fields[slot].id);
  const data = await request("GET", "/rest/api/2/issue/" + encodeURIComponent(key) + "?fields=" + ids.join(","), options);
  const read = (f) => {
    if (!f) return null;
    const v = data && data.fields ? data.fields[f.id] : null;
    if (v === null || v === undefined) return null;
    if (typeof v === "object") return v.value || v.name || JSON.stringify(v);
    return v;
  };
  return {
    ok: true,
    key,
    result: read(fields.result),
    selfCheck: read(fields.selfCheck),
    report: read(fields.report),
    checkedAt: read(fields.checkedAt),
  };
}

/** 自检：确认令牌、网络、账号与门禁字段可见性 */
export async function selftest(options = {}) {
  const result = { ok: false, steps: [] };
  const cred = options.token ? { token: options.token, source: "argument" } : await resolveToken(null);
  result.tokenSource = cred.source || null;
  result.checkedPaths = cred.checked || null;
  result.baseUrl = envLocked()
    ? (envValue("JIRA_BASE_URL") || DEFAULT_BASE_URL)
    : (options.baseUrl || cred.baseUrl || DEFAULT_BASE_URL);
  result.auth = String(cred.auth || "bearer").toLowerCase();
  if (!cred.token) {
    result.steps.push({ step: "读取令牌", ok: false, detail: "未找到令牌；请设置 JIRA_TOKEN 环境变量，或把令牌放到技能目录、~/.codex/jira-token.txt、~/.codex/jira/config.json" });
    return result;
  }
  result.steps.push({ step: "读取令牌", ok: true, detail: cred.source });
  try {
    const info = await request("GET", "/rest/api/2/serverInfo", { baseUrl: result.baseUrl });
    result.steps.push({ step: "访问 Jira", ok: true, detail: info.baseUrl + " v" + info.version });
  } catch (e) {
    result.steps.push({ step: "访问 Jira", ok: false, detail: e.message });
    return result;
  }
  try {
    const me = await myself({ baseUrl: result.baseUrl });
    result.steps.push({ step: "校验令牌", ok: true, detail: me.displayName + "（" + me.name + "）" });
    result.user = me.name;
  } catch (e) {
    // 部分站点禁止普通账号访问 /myself；只要站点可达就不要据此判定令牌失效。
    result.steps.push({ step: "校验令牌", ok: false, detail: e.message });
  }
  result.ok = true;
  return result;
}
