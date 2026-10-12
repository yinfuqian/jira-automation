/**
 * analysis.mjs —— 调用本工具（故障分析服务）HTTP 接口完成一次自动故障分析。
 *
 * 流程：解析产品/模块/精准 Tag → 校验远端 Tag → 上传缺陷单附件 → 提交异步分析 → 轮询进度 → 渲染 HTML 报告。
 * 服务地址与内部令牌来自环境变量（ANALYSIS_API_BASE_URL / ANALYSIS_API_TOKEN），
 * 也可通过命令行参数覆盖；令牌只放在请求头 X-API-Token，不打印、不写进报告。
 */
import { escapeHtml, renderAnalysisHtml } from "./report.mjs";
import {
  resolveGitTag,
  resolveModuleMapping,
  resolveProductMapping,
} from "./scope.mjs";

export const DEFAULT_BASE_URL = "http://127.0.0.1:5000";
export const IMAGE_EXTENSIONS = [".png", ".jpg", ".jpeg"];
/** 单次图片上传数量上限，与服务端 MAX_IMAGE_COUNT 默认值保持一致。 */
export const IMAGE_BATCH_SIZE = 10;
export const DEFAULT_TASK_TIMEOUT_SECONDS = 1800;

/** 读取环境变量（node_repl 下没有 process 时返回空串） */
export function envValue(name) {
  try {
    if (typeof process !== "undefined" && process.env && process.env[name]) {
      return String(process.env[name]);
    }
  } catch (e) { /* node_repl 无 process */ }
  return "";
}

/** 汇总接口地址与令牌，命令行参数优先于环境变量 */
export function resolveConfig(overrides = {}) {
  const baseUrl = String(overrides.baseUrl || envValue("ANALYSIS_API_BASE_URL") || DEFAULT_BASE_URL).replace(/\/+$/, "");
  const token = String(overrides.token || envValue("ANALYSIS_API_TOKEN") || "").trim();
  return { baseUrl, token, tokenSource: overrides.token ? "argument" : (token ? "environment" : null) };
}

/** 统一发起请求；未配置令牌时给出可执行的修复提示 */
export async function apiRequest(method, apiPath, options = {}) {
  const config = resolveConfig(options);
  if (!config.token) {
    throw new Error("未配置 ANALYSIS_API_TOKEN，无法调用故障分析接口；请在 .env 中设置 ANALYSIS_API_TOKEN 后重启 api 与 worker");
  }
  const headers = { Accept: "application/json", "X-API-Token": config.token };
  let payload;
  if (options.formData !== undefined) {
    payload = options.formData;
  } else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(options.body);
  }
  const target = /^https?:\/\//i.test(apiPath) ? apiPath : config.baseUrl + apiPath;
  const res = await fetch(target, { method, headers, body: payload, redirect: "follow" });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
  if (!res.ok) {
    let detail = "";
    if (data && typeof data === "object") {
      detail = data.error || data.message || JSON.stringify(data);
    } else if (typeof data === "string") {
      detail = data.slice(0, 300);
    }
    const err = new Error("故障分析接口 " + method + " " + apiPath + " 失败：HTTP " + res.status + (detail ? " — " + detail : ""));
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

/** 健康检查：确认地址可达、令牌可用 */
export async function selftest(options = {}) {
  const result = { ok: false, steps: [] };
  const config = resolveConfig(options);
  result.baseUrl = config.baseUrl;
  result.tokenSource = config.tokenSource;
  if (!config.token) {
    result.steps.push({ step: "读取内部令牌", ok: false, detail: "未配置 ANALYSIS_API_TOKEN" });
    return result;
  }
  result.steps.push({ step: "读取内部令牌", ok: true, detail: config.tokenSource });
  try {
    const data = await apiRequest("GET", "/product/get", options);
    const count = data && Array.isArray(data.products) ? data.products.length : 0;
    result.steps.push({ step: "访问故障分析服务", ok: true, detail: "产品数：" + count });
    result.ok = true;
  } catch (e) {
    result.steps.push({ step: "访问故障分析服务", ok: false, detail: e.message });
  }
  return result;
}

/** 按 id 或名称解析产品 */
export async function resolveProduct(value, options = {}) {
  const wanted = String(value || "").trim();
  if (!wanted) throw new Error("缺少产品（--product 产品名称或 id）");
  if (/^\d+$/.test(wanted)) return { id: wanted, name: "" };
  const data = await apiRequest("GET", "/product/get?name=" + encodeURIComponent(wanted), options);
  const products = (data && data.products) || [];
  const exact = products.find((item) => String(item.name || "").trim().toLowerCase() === wanted.toLowerCase())
    || (products.length === 1 ? products[0] : null);
  if (!exact) throw new Error("故障分析工具中没有唯一匹配的产品：" + wanted + "（请先同步或维护 GitLab 产品与模块）");
  return { id: String(exact.id), name: String(exact.name || "") };
}

/** 解析模块（返回模块 id、名称与该模块绑定的分支信息） */
export async function resolveModule(productId, value, options = {}) {
  const wanted = String(value || "").trim();
  if (!wanted) throw new Error("缺少模块（--module 模块名称或 id）");
  if (/^\d+$/.test(wanted)) {
    const data = await apiRequest("GET", "/module/get?product_id=" + encodeURIComponent(productId), options);
    const found = ((data && data.modules) || []).find((item) => String(item.module_id) === wanted);
    return { id: wanted, name: found ? String(found.module_name || "") : "", branch: found ? found.branch : null };
  }
  const data = await apiRequest("GET", "/module/search?names=" + encodeURIComponent(wanted), options);
  const modules = (data && data.modules) || [];
  const exact = modules.find((item) => String(item.module_name || "").trim().toLowerCase() === wanted.toLowerCase())
    || (modules.length === 1 ? modules[0] : null);
  if (!exact) throw new Error("故障分析工具中没有唯一匹配的模块：" + wanted + "（产品：" + productId + "）");
  return { id: String(exact.module_id), name: String(exact.module_name || ""), branch: exact.branch || null };
}

/**
 * 解析一次分析所需的定位参数：productId / moduleId / branchAddress / tagVersion。
 * branchAddress 实际是仓库地址；tagVersion 只接受 Git Tag。
 * 仓库地址优先取显式输入，其次取 scope-mapping.json；不再回落到工具里的分支绑定。
 */
async function resolveScopeEntry(input = {}, options = {}, resolvedProduct = null, allowCommonWithoutTag = false) {
  const product = resolvedProduct || await resolveProduct(input.product, options);
  const module = await resolveModule(product.id, input.module, options);
  let configuredRepo = "";
  let configuredRef = "";
  let configuredCommon = false;
  let configError = null;
  try {
    const productMapping = resolveProductMapping(input.product, null, options.configPath);
    const moduleMapping = resolveModuleMapping(productMapping, input.module);
    configuredRepo = moduleMapping.repoUrl;
    configuredCommon = Boolean(moduleMapping.value.common);
    if (input.version) {
      configuredRef = resolveGitTag(productMapping, moduleMapping, input.version, null, options.configPath).gitTag;
    }
  } catch (error) {
    configError = error;
  }
  const explicitFullScope = Boolean(input.branch && (input.ref || input.version));
  if (configError && !explicitFullScope) {
    throw new Error("分析范围映射失败：" + configError.message + "；如确需人工指定，请同时传 --branch 与 --version");
  }
  const branchAddress = String(input.branch || configuredRepo || "").trim();
  const tagVersion = String(input.ref || configuredRef || input.version || "").trim();
  const commonWithoutTag = configuredCommon && allowCommonWithoutTag && !tagVersion;
  if (!branchAddress || (!tagVersion && !commonWithoutTag)) {
    const detail = configError ? "；映射配置错误：" + configError.message : "";
    throw new Error("缺少代码仓库地址或 Tag：请从 Jira 读取产品、模块与 Tag 版本，或用 --branch / --version 指定（模块：" + module.name + "）" + detail);
  }
  return {
    productId: product.id,
    productName: product.name,
    moduleId: module.id,
    moduleName: module.name,
    branchAddress,
    tagVersion,
    common: configuredCommon,
  };
}

/**
 * 解析主模块与可选关联模块。关联模块共用主模块的产品。
 */
export async function resolveScope(input = {}, options = {}) {
  const product = await resolveProduct(input.product, options);
  const primary = await resolveScopeEntry(input, options, product);
  const relatedModules = [];
  const seen = new Set([String(primary.moduleId)]);
  for (const item of input.related || []) {
    const related = await resolveScopeEntry({ ...item, product: input.product }, options, product, true);
    if (seen.has(String(related.moduleId))) continue;
    seen.add(String(related.moduleId));
    const relatedEntry = {
      moduleId: related.moduleId,
      moduleName: related.moduleName,
      branchAddress: related.branchAddress,
      tagVersion: related.tagVersion,
    };
    if (related.common) relatedEntry.common = true;
    relatedModules.push(relatedEntry);
  }
  return { ...primary, relatedModules };
}

/**
 * 校验主模块与关联模块使用的都是远端真实存在的精准 Tag。
 * 该检查在提交分析前完成，找不到 Tag 时由门禁按缺参打回。
 */
export async function validateScopeTags(scope, options = {}) {
  if (!scope) throw new Error("缺少分析范围，无法校验精准 Tag");
  const relatedModules = (scope.relatedModules || []).map((item) => ({
    moduleId: item.moduleId,
    moduleName: item.moduleName,
    branchAddress: item.branchAddress,
    tagVersion: item.tagVersion,
    common: item.common,
  }));
  const result = await apiRequest("POST", "/analysis/validate_refs", {
    ...options,
    body: {
      productId: scope.productId,
      moduleId: scope.moduleId,
      moduleName: scope.moduleName,
      branchAddress: scope.branchAddress,
      tagVersion: scope.tagVersion,
      relatedModules,
    },
  });
  if (!result || result.ok !== true) {
    const details = ((result && result.missing) || [])
      .map((item) => `${item.moduleName || "unknown"}:${item.tagVersion || "（未提供 Tag）"}${item.reason ? `（${item.reason}）` : ""}`)
      .join("；");
    throw new Error("精准 Git Tag 校验失败：" + (details || "远端仓库中找不到指定 Tag"));
  }
  return result;
}

/** 按扩展名把附件目录分成图片与日志两类 */
export function classifyFiles(filePaths, fsModule = null) {
  const images = [];
  const logs = [];
  const unreadable = [];
  for (const file of filePaths || []) {
    const path = String(file || "");
    if (!path) continue;
    const lower = path.toLowerCase();
    const isImage = IMAGE_EXTENSIONS.some((ext) => lower.endsWith(ext));
    const info = { path, filename: path.replace(/^.*[\\/]/, "") };
    if (fsModule) {
      try {
        info.size = fsModule.statSync(path).size;
      } catch (e) {
        unreadable.push({ ...info, reason: String((e && e.message) || e) });
        continue;
      }
    }
    if (isImage) images.push(info);
    else logs.push(info);
  }
  return { images, logs, unreadable };
}

/** 列目录下的普通文件（不递归） */
export async function listDirectory(dir) {
  const fs = await import("node:fs");
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir)
    .map((name) => dir.replace(/\/+$/, "") + "/" + name)
    .filter((path) => {
      try { return fs.statSync(path).isFile(); } catch (e) { return false; }
    });
}

/** 以 multipart 形式上传图片（服务端字段名 files + image_tag） */
export async function uploadImages(scope, images, options = {}) {
  const fs = await import("node:fs");
  const uploaded = [];
  for (let start = 0; start < images.length; start += IMAGE_BATCH_SIZE) {
    const batch = images.slice(start, start + IMAGE_BATCH_SIZE);
    const form = new FormData();
    form.append("product_id", String(scope.productId));
    form.append("module_id", String(scope.moduleId));
    form.append("address", String(scope.branchAddress));
    form.append("tag_version", String(scope.tagVersion));
    form.append("image_tag", String(options.imageTag || "log_image"));
    form.append("image_description", String(options.imageDescription || ""));
    for (const image of batch) {
      form.append("files", new Blob([fs.readFileSync(image.path)]), image.filename);
    }
    const data = await apiRequest("POST", "/logfile/upload_image", { ...options, formData: form });
    uploaded.push({
      log_ids: data.log_ids || [],
      file_paths: data.file_paths || [],
      image_tag: data.image_tag,
      image_description: data.image_description,
      files: batch.map((item) => item.filename),
    });
  }
  const merged = {
    log_ids: [],
    file_paths: [],
    image_tag: String(options.imageTag || "log_image"),
    image_description: String(options.imageDescription || ""),
    files: [],
  };
  for (const item of uploaded) {
    merged.log_ids.push(...item.log_ids);
    merged.file_paths.push(...item.file_paths);
    merged.files.push(...item.files);
  }
  return merged;
}

/** 上传日志文件（服务端字段名 file） */
export async function uploadLogs(scope, logs, options = {}) {
  const fs = await import("node:fs");
  const uploaded = { log_ids: [], file_paths: [], files: [] };
  for (const log of logs) {
    const form = new FormData();
    form.append("product_id", String(scope.productId));
    form.append("module_id", String(scope.moduleId));
    form.append("address", String(scope.branchAddress));
    form.append("tag_version", String(scope.tagVersion));
    if (options.dateFilter) form.append("date_filter", String(options.dateFilter));
    form.append("file", new Blob([fs.readFileSync(log.path)]), log.filename);
    const data = await apiRequest("POST", "/logfile/upload", { ...options, formData: form });
    uploaded.log_ids.push(data.log_id);
    uploaded.file_paths.push(data.file_path);
    uploaded.files.push(log.filename);
  }
  return uploaded;
}

/** 提交异步分析任务 */
export async function submitAnalysis(payload, options = {}) {
  return await apiRequest("POST", "/analysis/submit_async", { ...options, body: payload });
}

/** 轮询任务直到结束，onProgress 用于输出进度 */
export async function waitForTask(taskId, options = {}) {
  const timeoutSeconds = Number(options.timeoutSeconds || DEFAULT_TASK_TIMEOUT_SECONDS);
  const intervalMs = Number(options.intervalMs || 5000);
  const startedAt = Date.now();
  let lastMessage = "";
  while (true) {
    const data = await apiRequest("GET", "/analysis/task/" + encodeURIComponent(taskId), options);
    const progress = data && data.progress;
    if (progress && typeof options.onProgress === "function") {
      const message = String(progress.message || progress.stage_label || "");
      if (message && message !== lastMessage) {
        lastMessage = message;
        options.onProgress({ stage: progress.stage, message, percent: progress.percent });
      }
    }
    if (data && data.ready) {
      if (data.successful) return data.result || {};
      throw new Error("分析任务失败：" + String((data && data.error) || "服务未返回失败原因"));
    }
    if ((Date.now() - startedAt) / 1000 > timeoutSeconds) {
      throw new Error("分析任务超过 " + timeoutSeconds + " 秒仍未结束（task_id=" + taskId + "），请稍后在工具前端查看该任务结果");
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}

/**
 * 完整跑一次分析：上传附件 → 提交任务 → 等待结果。
 * 日志与图片会分别提交两个分析任务，最后合并进同一份 HTML 报告。
 */
export async function analyzeDirectory(dir, input = {}, options = {}) {
  const fs = await import("node:fs");
  const scope = input.scope || await resolveScope(input, options);
  const files = input.files || await listDirectory(dir);
  const classified = classifyFiles(files, fs);
  const tasks = [];
  const notes = [];

  if (!classified.images.length && !classified.logs.length) {
    throw new Error("附件目录中没有可分析的日志或图片文件：" + dir);
  }
  for (const item of classified.unreadable) {
    notes.push("附件读取失败：" + item.filename + "（" + item.reason + "）");
  }

  if (classified.logs.length) {
    const upload = await uploadLogs(scope, classified.logs, options);
    const submit = await submitAnalysis({
      productId: scope.productId,
      moduleId: scope.moduleId,
      branchAddress: scope.branchAddress,
      tagVersion: scope.tagVersion,
      relatedModules: scope.relatedModules || [],
      source_type: "file",
      log_ids: upload.log_ids,
      file_path: upload.file_paths[0],
    }, options);
    tasks.push({ label: "日志分析", taskId: submit.task_id, files: upload.files });
  }
  if (classified.images.length) {
    const upload = await uploadImages(scope, classified.images, options);
    const submit = await submitAnalysis({
      productId: scope.productId,
      moduleId: scope.moduleId,
      branchAddress: scope.branchAddress,
      tagVersion: scope.tagVersion,
      relatedModules: scope.relatedModules || [],
      source_type: "image",
      image_tag: upload.image_tag,
      image_description: upload.image_description,
      log_ids: upload.log_ids,
      file_paths: upload.file_paths,
    }, options);
    tasks.push({ label: "图片分析", taskId: submit.task_id, files: upload.files });
  }

  const finished = [];
  for (const task of tasks) {
    try {
      const result = await waitForTask(task.taskId, options);
      finished.push({ ...task, result });
    } catch (e) {
      finished.push({ ...task, error: String((e && e.message) || e) });
    }
  }
  return {
    scope,
    files: [...classified.images, ...classified.logs].map((item) => ({
      filename: item.filename,
      kind: classified.images.includes(item) ? "图片" : "日志",
      size: item.size,
    })),
    tasks: finished,
    notes,
  };
}

/** 写 HTML 报告到磁盘，返回文件路径 */
export async function writeReport(payload, outPath) {
  const fs = await import("node:fs");
  const html = renderAnalysisHtml(payload);
  fs.writeFileSync(outPath, html, "utf8");
  return outPath;
}

/** 摘出结论，供 Jira 评论使用（无结果时返回空串） */
export function buildSummary(payload = {}) {
  const lines = [];
  for (const task of payload.tasks || []) {
    const result = task.result || {};
    const conclusion = result.issue_conclusion || {};
    if (task.error) {
      lines.push(task.label + "：分析失败（" + task.error + "）");
      continue;
    }
    const bits = [];
    if (conclusion.issue_category_label) bits.push("分类：" + conclusion.issue_category_label);
    if (conclusion.issue_count !== undefined) bits.push("问题数：" + conclusion.issue_count);
    if (conclusion.confidence !== undefined) bits.push("置信度：" + Math.round(Number(conclusion.confidence || 0) * 100) + "%");
    lines.push(task.label + "：" + (String(conclusion.conclusion_summary || "").trim() || "（模型未返回一句话结论）") + (bits.length ? "（" + bits.join("，") + "）" : ""));
  }
  return lines.join("\n");
}

export { escapeHtml, renderAnalysisHtml };
