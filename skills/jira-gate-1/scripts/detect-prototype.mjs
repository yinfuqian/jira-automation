#!/usr/bin/env node
/**
 * detect-prototype.mjs —— 判定本次 Jira 单是否上传了可审核的原型材料。
 *
 * 用法：
 *   node scripts/detect-prototype.mjs --dir <附件目录> [--json]
 *
 * 背景：G1 只在单子里确实上传了原型时才调用 review-jira-design；没有原型时这一步
 * 记为「不适用」，不生成、不上传原型设计准入报告，也不参与综合结论。
 *
 * 判定口径（宁可多报候选、不可静默跳过）：
 *   1. 文件名或相对路径含原型关键词（原型 / prototype / mockup / axure / figma / 蓝湖 / 墨刀 / 交互 等）→ 命中。
 *   2. 图片附件（png/jpg/jpeg/gif/webp/bmp/svg），只要文件名不像 PRD、评审报告、技术方案等文档 → 命中。
 *   3. HTML/HTM 附件：路径命中关键词，或前 64KB 内容里出现原型关键词 → 命中；
 *      否则记入 ambiguous（交由调用方打开确认），不直接当作原型。
 *   4. 未展开的压缩包（zip/rar/7z）在没有任何命中时记入 ambiguous，提示先解压再判定。
 *
 * 输出：单行 JSON，字段 hasPrototype / reason / hits / ambiguous；脚本始终 exit 0。
 */

import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { extname, join, resolve } from "node:path";


/** 原型关键词：命中文件名、相对路径或 HTML 正文即视为原型材料。 */
const PROTOTYPE_KEYWORDS = [
  "原型",
  "prototype",
  "mockup",
  "wireframe",
  "axure",
  "figma",
  "lanhu",
  "蓝湖",
  "modao",
  "墨刀",
  "交互",
  "演示页面",
  "页面示意",
];

/** 文档关键词：命中且不含原型关键词时，不当作原型（避免把 PRD/评审报告误判成原型）。 */
const DOCUMENT_KEYWORDS = [
  "prd",
  "需求文档",
  "需求说明",
  "需求评审",
  "评审报告",
  "准入报告",
  "技术方案",
  "测试方案",
  "测试用例",
  "overview",
  "概览",
];

/** 图片扩展名：原型截图通常以图片附件形式上传递。 */
const IMAGE_EXTS = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"]);

/** HTML 扩展名：业务原型初稿的标准交付格式。 */
const HTML_EXTS = new Set([".html", ".htm"]);

/** 压缩包扩展名：未展开时无法判定内部是否有原型。 */
const ARCHIVE_EXTS = new Set([".zip", ".rar", ".7z"]);

/** 纯中间产物与文档格式，不参与原型判定。 */
const SKIP_EXTS = new Set([".txt", ".json", ".log", ".md", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv"]);

/** HTML 正文嗅探上限：只看开头，避免把大文件整份读进内存。 */
const SNIFF_BYTES = 64 * 1024;

/** 递归遍历的深度与数量上限，防止异常目录拖垮任务。 */
const MAX_DEPTH = 8;
const MAX_FILES = 5000;


/** 判断文本是否含原型关键词（大小写不敏感）。 */
function hasPrototypeKeyword(text) {
  const value = String(text || "").toLowerCase();
  return PROTOTYPE_KEYWORDS.some((keyword) => value.includes(keyword));
}


/** 判断文本是否像文档（PRD / 评审报告 / 技术方案等）。 */
function looksLikeDocument(text) {
  const value = String(text || "").toLowerCase();
  return DOCUMENT_KEYWORDS.some((keyword) => value.includes(keyword));
}


/** 读取 HTML 开头内容用于关键词嗅探；读失败时返回空串，不影响其它文件判定。 */
function sniffHtml(file) {
  try {
    const fd = readFileSync(file);
    return fd.subarray(0, SNIFF_BYTES).toString("utf8");
  } catch {
    return "";
  }
}


/** 递归收集目录下的文件（相对路径 + 绝对路径），跳过隐藏目录与超深层级。 */
function collectFiles(root) {
  const files = [];
  const walk = (dir, depth) => {
    if (depth > MAX_DEPTH || files.length >= MAX_FILES) return;
    let entries;
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (files.length >= MAX_FILES) return;
      if (entry.name.startsWith(".")) continue;
      const full = join(dir, entry.name);
      if (entry.isDirectory()) {
        walk(full, depth + 1);
      } else if (entry.isFile()) {
        files.push({ full, rel: full.slice(root.length + 1) });
      }
    }
  };
  walk(root, 0);
  return files;
}


/** 解析命令行参数，支持 --dir <目录> 与 --json。 */
function parseArgs(argv) {
  const options = { json: false, dir: "" };
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (item === "--json") options.json = true;
    else if (item === "--dir") {
      options.dir = argv[index + 1] || "";
      index += 1;
    }
  }
  return options;
}


/** 主判定：返回命中列表与待人工确认列表。 */
function detect(dir) {
  const root = resolve(dir);
  if (!existsSync(root) || !statSync(root).isDirectory()) {
    return { hasPrototype: false, reason: "附件目录不存在，无法判定是否上传原型", hits: [], ambiguous: [] };
  }

  const hits = [];
  const ambiguous = [];
  for (const file of collectFiles(root)) {
    const ext = extname(file.rel).toLowerCase();
    const label = file.rel.toLowerCase();
    if (SKIP_EXTS.has(ext)) continue;

    const prototypeByPath = hasPrototypeKeyword(label);
    const documentByPath = looksLikeDocument(label) && !prototypeByPath;

    if (ARCHIVE_EXTS.has(ext)) {
      ambiguous.push({ path: file.rel, kind: "archive" });
      continue;
    }
    if (documentByPath) continue;

    if (prototypeByPath) {
      hits.push({ path: file.rel, kind: "path-keyword" });
      continue;
    }
    if (IMAGE_EXTS.has(ext)) {
      hits.push({ path: file.rel, kind: "image" });
      continue;
    }
    if (HTML_EXTS.has(ext)) {
      if (hasPrototypeKeyword(sniffHtml(file.full))) hits.push({ path: file.rel, kind: "html-content" });
      else ambiguous.push({ path: file.rel, kind: "html-unlabeled" });
    }
  }

  const hasPrototype = hits.length > 0;
  const reason = hasPrototype
    ? `命中 ${hits.length} 个原型材料：${hits.map((item) => item.path).slice(0, 5).join("、")}`
    : ambiguous.length
      ? "未识别出原型材料，但存在需人工确认的 HTML/压缩包"
      : "附件中没有原型材料";
  return { hasPrototype, reason, hits, ambiguous };
}


function main() {
  const options = parseArgs(process.argv.slice(2));
  const result = detect(options.dir || ".");
  const payload = { ok: true, dir: options.dir || ".", ...result };
  if (options.json) {
    process.stdout.write(`${JSON.stringify(payload)}\n`);
    return;
  }
  // 非 JSON 模式给出便于肉眼核对的摘要，判定结果同时以 JSON 行输出，供脚本消费。
  process.stdout.write(`HAS_PROTOTYPE=${payload.hasPrototype}\n`);
  process.stdout.write(`PROTOTYPE_REASON=${payload.reason}\n`);
  process.stdout.write(`PROTOTYPE_JSON=${JSON.stringify(payload)}\n`);
}


main();