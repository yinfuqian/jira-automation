#!/usr/bin/env node
/**
 * pack-deliverables.mjs —— 把 jira-gate-1-mock 本轮预检的交付物打成压缩包。
 *
 * 用法：
 *   node scripts/pack-deliverables.mjs --key <KEY> --comment <评论草稿文件> --report <报告md> \
 *     [--design <原型设计准入报告html>] [--out <输出目录>] [--zip-name <压缩包名>] [--extra <附加文件> ...]
 *
 * 背景：本技能是 jira-gate-1 的预演版，对 Jira 只读——不写评论、不写门禁字段、不上传附件、不流转状态。
 * 交付物因此改为一个压缩包，内含：
 *   1. <KEY>-G1预检评论.txt          评论草稿（Jira wiki 标记，可直接粘贴到需求单评论区）
 *   2. <KEY>-需求评审报告-宋立志.md   评审报告（与 jira-gate-1 上传附件时使用的规范文件名一致）
 *   3. <KEY>-原型设计准入报告.html    PRD 与原型一致性审核报告（--design 指定来源，见 SKILL.md §11）
 *   4. --extra 指定的附加文件（可选，保持原文件名）
 *
 * 输出：默认写到 <工作目录>/outputs/<KEY>-G1预检包-<时间戳>.zip，并在 stdout 打印 JSON 结果。
 *
 * 只读约束：脚本只读本地文件，不发起任何网络请求，也不碰 Jira。
 * 打包时会对评论草稿与设计报告做一次轻量校验（Markdown 标记残留、缺检查项标记、HTML 未替换占位符等），
 * 只警告不阻断，便于在交付前发现格式问题。
 */

import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { basename, join, resolve } from "node:path";
import { deflateRawSync } from "node:zlib";


/** 打包失败时抛出，带可读 reason 便于调用方与测试判断。 */
class PackError extends Error {
  constructor(message, reason) {
    super(message);
    this.name = "PackError";
    this.reason = reason || "unknown";
  }
}


/** ZIP 用的 CRC-32 查找表（多项式 0xEDB88320） */
const CRC_TABLE = (() => {
  const table = new Int32Array(256);
  for (let index = 0; index < 256; index += 1) {
    let value = index;
    for (let bit = 0; bit < 8; bit += 1) {
      value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
    }
    table[index] = value;
  }
  return table;
})();


/** 计算 ZIP 条目要求的 CRC-32 校验值 */
function crc32(buffer) {
  let crc = -1;
  for (let index = 0; index < buffer.length; index += 1) {
    crc = (crc >>> 8) ^ CRC_TABLE[(crc ^ buffer[index]) & 0xff];
  }
  return (crc ^ -1) >>> 0;
}


/** 把时间转换成 ZIP 头使用的 DOS 时间戳 */
function dosDateTime(date) {
  return {
    time: ((date.getHours() << 11) | (date.getMinutes() << 5) | Math.floor(date.getSeconds() / 2)) & 0xffff,
    date: (((date.getFullYear() - 1980) << 9) | ((date.getMonth() + 1) << 5) | date.getDate()) & 0xffff,
  };
}


/**
 * 生成标准 ZIP 字节流（deflate 压缩，文件名按 UTF-8 写并置 UTF-8 标志位）。
 *
 * 自己实现而不是调用系统命令：技能在容器与 Windows 桌面端都要能跑，
 * `zip` / `Compress-Archive` 在两个环境里并不都可用，而 node:zlib 是内置能力。
 */
function buildZip(entries, now) {
  const stamp = dosDateTime(now);
  const parts = [];
  const centralParts = [];
  let offset = 0;

  for (const entry of entries) {
    const nameBuffer = Buffer.from(entry.name, "utf8");
    const raw = entry.data;
    const crc = crc32(raw);
    // 压缩后反而更大时退回 store，避免小文本文件被撑大。
    const deflated = deflateRawSync(raw, { level: 9 });
    const useDeflate = deflated.length < raw.length;
    const payload = useDeflate ? deflated : raw;
    const method = useDeflate ? 8 : 0;

    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0x0800, 6);
    local.writeUInt16LE(method, 8);
    local.writeUInt16LE(stamp.time, 10);
    local.writeUInt16LE(stamp.date, 12);
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(payload.length, 18);
    local.writeUInt32LE(raw.length, 22);
    local.writeUInt16LE(nameBuffer.length, 26);
    local.writeUInt16LE(0, 28);
    parts.push(local, nameBuffer, payload);

    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt16LE(0x0800, 8);
    central.writeUInt16LE(method, 10);
    central.writeUInt16LE(stamp.time, 12);
    central.writeUInt16LE(stamp.date, 14);
    central.writeUInt32LE(crc, 16);
    central.writeUInt32LE(payload.length, 20);
    central.writeUInt32LE(raw.length, 24);
    central.writeUInt16LE(nameBuffer.length, 28);
    central.writeUInt16LE(0, 30);
    central.writeUInt16LE(0, 32);
    central.writeUInt16LE(0, 34);
    central.writeUInt16LE(0, 36);
    central.writeUInt32LE(0, 38);
    central.writeUInt32LE(offset, 42);
    centralParts.push(central, nameBuffer);

    offset += local.length + nameBuffer.length + payload.length;
  }

  const centralBuffer = Buffer.concat(centralParts);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(0, 4);
  end.writeUInt16LE(0, 6);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(centralBuffer.length, 12);
  end.writeUInt32LE(offset, 16);
  end.writeUInt16LE(0, 20);
  return Buffer.concat([...parts, centralBuffer, end]);
}


/**
 * 校验评论草稿的 Jira wiki 标记；只返回警告，不阻断打包。
 *
 * 注意：不要按「行首 #」判 Markdown 标题——Jira wiki 的有序列表就是行首 `# `（检查项清单正是这种写法），
 * 两者同形，只有 `**粗体**` 这类明确不属于 wiki 的写法才值得提醒。
 */
function checkCommentWiki(text) {
  const warnings = [];
  if (!/^h[1-6]\.\s/m.test(text)) {
    warnings.push("评论里没有 h3./h4. 标题，Jira 渲染不出小标题");
  }
  if (/\*\*[^*\n]+\*\*/.test(text)) {
    warnings.push("评论里出现 Markdown 粗体（**文字**），Jira wiki 标记应写成「*文字*」");
  }
  if (/^\|.*\|$/m.test(text) && !/^\|\|/.test(text)) {
    warnings.push("评论里出现 Markdown 表格（单竖线表头），Jira wiki 应写成「||列名||」");
  }
  if (!text.includes("(/)") && !text.includes("(x)")) {
    warnings.push("评论里没有任何检查项标记（(/) 或 (x)），通过项也要写 (/)");
  }
  if (!text.includes("报告附件")) {
    warnings.push("评论末尾缺少「报告附件」一行");
  }
  return warnings;
}


/**
 * 校验原型设计准入报告 HTML；只返回警告，不阻断打包。
 *
 * 该报告由 review-jira-design 按自己的模板生成，这里只做「有没有交半成品」的粗检：
 * 占位符没替换完、或四档结论一个都没有，都会在交付前被拦一道提示。
 */

// review-jira-design 的结论只有这四档；报告里必须能读到其中一档。
const DESIGN_CONCLUSIONS = ["退回补齐", "有条件通过", "通过", "审核未完成"];

function checkDesignReport(bytes) {
  const warnings = [];
  const text = Buffer.isBuffer(bytes) ? bytes.toString("utf8") : String(bytes ?? "");
  if (!/<html[\s>]/i.test(text)) {
    warnings.push("设计报告看起来不是 HTML（找不到 <html> 标签）");
    return warnings;
  }
  const leftovers = [...text.matchAll(/\{\{[^}]{1,40}\}\}/g)].map((match) => match[0]);
  if (leftovers.length) {
    const sample = [...new Set(leftovers)].slice(0, 5).join(" ");
    warnings.push(`设计报告里还有未替换的模板占位符 ${leftovers.length} 处（如 ${sample}）`);
  }
  if (!DESIGN_CONCLUSIONS.some((item) => text.includes(item))) {
    warnings.push(`设计报告里找不到结论（应为 ${DESIGN_CONCLUSIONS.join(" / ")} 之一）`);
  }
  return warnings;
}


/** 读取必须存在的证据文件，缺失时抛出可读错误 */
function readRequiredFile(filePath, label) {
  if (!filePath) throw new PackError(`缺少 ${label} 路径`, "missing-argument");
  const absolute = resolve(String(filePath));
  if (!existsSync(absolute) || !statSync(absolute).isFile()) {
    throw new PackError(`${label}不存在或不是文件：${absolute}`, "file-not-found");
  }
  return { path: absolute, data: readFileSync(absolute) };
}


/** 生成 <YYYYMMDD-HHmmss> 形式的时间戳，避免同名压缩包互相覆盖 */
function timestampOf(date) {
  const pad = (value) => String(value).padStart(2, "0");
  return (
    `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}` +
    `-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`
  );
}


/** 执行打包，返回可直接打印为 JSON 的结果对象 */
function pack(options) {
  const key = String(options.key || "").trim().toUpperCase();
  if (!key) throw new PackError("缺少 --key（需求单号）", "key-missing");
  if (!/^[A-Z][A-Z0-9_]*-\d+$/.test(key)) {
    throw new PackError(`单号格式不合法：${key}`, "key-invalid");
  }

  const comment = readRequiredFile(options.comment, "评论草稿");
  const report = readRequiredFile(options.report, "评审报告");
  // 设计准入报告是流程必需项（SKILL.md §11），但参数层保持可选：
  // 该需求确实没有原型时，报告会以「审核未完成」结论交付，仍应进包。
  const design = options.design ? readRequiredFile(options.design, "原型设计准入报告") : null;
  const extras = [];
  for (const item of [].concat(options.extra || [])) {
    const file = readRequiredFile(item, "附加文件");
    extras.push({ name: basename(file.path), data: file.data });
  }

  const entries = [
    { name: `${key}-G1预检评论.txt`, data: comment.data },
    { name: `${key}-需求评审报告-宋立志.md`, data: report.data },
    ...(design ? [{ name: `${key}-原型设计准入报告.html`, data: design.data }] : []),
    ...extras,
  ];

  const outDir = resolve(String(options.out || "outputs"));
  mkdirSync(outDir, { recursive: true });
  const zipName = String(options["zip-name"] || "").trim() || `${key}-G1预检包-${timestampOf(new Date())}.zip`;
  const zipPath = join(outDir, zipName);
  const zipBuffer = buildZip(entries, new Date());
  writeFileSync(zipPath, zipBuffer);

  const warnings = checkCommentWiki(comment.data.toString("utf8"));
  if (design) warnings.push(...checkDesignReport(design.data));

  return {
    ok: true,
    key,
    zip: zipPath,
    zipBytes: zipBuffer.length,
    entries: entries.map((entry) => ({ name: entry.name, bytes: entry.data.length })),
    warnings,
  };
}


/** 解析命令行参数：--key value 形式，--extra 可重复 */
function parseArgs(argv) {
  const options = {};
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (!item.startsWith("--")) continue;
    const name = item.slice(2);
    const next = argv[index + 1];
    if (next === undefined || String(next).startsWith("--")) {
      options[name] = true;
      continue;
    }
    if (name === "extra") {
      options.extra = [].concat(options.extra || [], next);
    } else {
      options[name] = next;
    }
    index += 1;
  }
  return options;
}


function main() {
  const options = parseArgs(process.argv.slice(2));
  if (!options.key || !options.comment || !options.report) {
    throw new PackError(
      "用法：node scripts/pack-deliverables.mjs --key <KEY> --comment <评论草稿> --report <报告md> [--design <原型设计准入报告html>] [--out <目录>] [--zip-name <名>] [--extra <文件>]",
      "missing-argument",
    );
  }
  process.stdout.write(JSON.stringify(pack(options), null, 2) + "\n");
}


// 被其它脚本作为库导入时不执行命令行逻辑。
try {
  main();
} catch (error) {
  const reason = error && error.reason ? `（${error.reason}）` : "";
  process.stderr.write(`[打包失败]${reason} ${(error && error.message) || error}\n`);
  process.exitCode = 1;
}
