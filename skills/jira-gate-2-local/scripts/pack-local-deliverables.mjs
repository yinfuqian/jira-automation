#!/usr/bin/env node
/**
 * 打包 jira-gate-2-local 的本地预检交付物。
 *
 * 用法：
 *   node scripts/pack-local-deliverables.mjs --key <KEY> --comment <草稿> --pdca <报告md> \
 *     [--no-pdca] [--extra <附加文件>] \
 *     [--out <输出目录>] [--zip-name <压缩包名>] [--extra <附加文件>]
 *
 * 安全约束：脚本只读本地文件并写 outputs，不发起网络请求，不触碰 Jira。
 */

import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { basename, join, resolve } from "node:path";
import { deflateRawSync } from "node:zlib";

/** 打包失败错误，reason 供调用方定位参数或文件问题。 */
class PackError extends Error {
  constructor(message, reason) {
    super(message);
    this.name = "PackError";
    this.reason = reason || "unknown";
  }
}

/** ZIP CRC-32 查找表。 */
const CRC_TABLE = (() => {
  const table = new Int32Array(256);
  for (let index = 0; index < 256; index += 1) {
    let value = index;
    for (let bit = 0; bit < 8; bit += 1) value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
    table[index] = value;
  }
  return table;
})();

/** 计算 ZIP CRC-32。 */
function crc32(buffer) {
  let crc = -1;
  for (const byte of buffer) crc = (crc >>> 8) ^ CRC_TABLE[(crc ^ byte) & 0xff];
  return (crc ^ -1) >>> 0;
}

/** 生成 ZIP 头使用的 DOS 时间。 */
function dosDateTime(date) {
  return {
    time: ((date.getHours() << 11) | (date.getMinutes() << 5) | Math.floor(date.getSeconds() / 2)) & 0xffff,
    date: (((date.getFullYear() - 1980) << 9) | ((date.getMonth() + 1) << 5) | date.getDate()) & 0xffff,
  };
}

/** 用 Node 内置 zlib 生成 ZIP，兼容 Windows 与容器。 */
function buildZip(entries, now) {
  const stamp = dosDateTime(now);
  const localParts = [];
  const centralParts = [];
  let offset = 0;
  for (const entry of entries) {
    const nameBuffer = Buffer.from(entry.name, "utf8");
    const raw = entry.data;
    const crc = crc32(raw);
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
    localParts.push(local, nameBuffer, payload);

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
    central.writeUInt32LE(offset, 42);
    centralParts.push(central, nameBuffer);
    offset += local.length + nameBuffer.length + payload.length;
  }

  const centralOffset = offset;
  const centralSize = centralParts.reduce((sum, part) => sum + part.length, 0);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(centralSize, 12);
  end.writeUInt32LE(centralOffset, 16);
  return Buffer.concat([...localParts, ...centralParts, end]);
}

/** 读取存在的本地文件。 */
function readRequiredFile(filePath, label) {
  if (!filePath) throw new PackError(`缺少${label}路径`, "path-missing");
  const absolute = resolve(String(filePath));
  if (!existsSync(absolute) || !statSync(absolute).isFile()) {
    throw new PackError(`${label}不存在或不是文件：${absolute}`, "file-not-found");
  }
  return { path: absolute, data: readFileSync(absolute) };
}

/** 生成 YYYYMMDD-HHmmss 时间戳。 */
function timestampOf(date) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
}

/** 删除同需求旧预检包，只保留本次包。 */
function removePreviousPackages(outDir, key, keepPath) {
  const keep = resolve(keepPath);
  const pattern = new RegExp(`^${key}-G2本地预检包-.*\\.zip$`, "i");
  const removed = [];
  for (const name of readdirSync(outDir)) {
    if (!pattern.test(name)) continue;
    const full = join(outDir, name);
    if (resolve(full) === keep) continue;
    try {
      rmSync(full, { force: true });
      removed.push(name);
    } catch (error) {
      process.stderr.write(`[打包警告] 旧包删除失败：${full}（${error.message}）\n`);
    }
  }
  return removed;
}

/** 校验评论草稿为 Jira wiki 标记并带本地预检标识；G2 评论本身不要求逐项勾选标记。 */
function checkComment(text, warnings) {
  if (!text.includes("（本地预检）")) warnings.push("评论草稿缺少「（本地预检）」标题标识");
  if (/\*\*[^*]+\*\*|^#{1,6}\s+/m.test(text)) warnings.push("评论草稿疑似包含 Markdown 语法");
  if (!/G2 技术方案准出结论/.test(text)) warnings.push("评论草稿缺少 G2 技术方案准出结论标题");
}

/** 校验报告文件名保留 PDCA RD 报告语义且内容非空。 */
function checkReport(name, data, warnings) {
  if (!name.endsWith("-PDCA-RD报告.md")) warnings.push(`PDCA报告名不符合规范：${name}`);
  if (!data.length) warnings.push("PDCA报告为空文件");
}

/** 执行打包并返回 JSON 结果。 */
function pack(options) {
  const key = String(options.key || "").trim().toUpperCase();
  if (!/^[A-Z][A-Z0-9_]*-\d+$/.test(key)) throw new PackError(`单号格式不合法：${key}`, "key-invalid");

  const comment = readRequiredFile(options.comment, "评论草稿");
  const noPdca = options["no-pdca"] === true;
  const pdca = noPdca ? null : readRequiredFile(options.pdca, "PDCA报告");

  const entries = [{ name: `${key}-G2预检评论.txt`, data: comment.data }];
  if (pdca) {
    // 包内统一归一化文件名，避免本地草稿名与最终交付名不一致。
    entries.push({ name: `${key}-PDCA-RD报告.md`, data: pdca.data });
  }

  for (const item of [].concat(options.extra || [])) {
    const file = readRequiredFile(item, "附加文件");
    entries.push({ name: basename(file.path), data: file.data });
  }

  const warnings = [];
  checkComment(comment.data.toString("utf8"), warnings);
  if (pdca) checkReport(`${key}-PDCA-RD报告.md`, pdca.data, warnings);

  const outDir = resolve(String(options.out || "outputs"));
  mkdirSync(outDir, { recursive: true });
  const zipName = String(options["zip-name"] || "").trim() || `${key}-G2本地预检包-${timestampOf(new Date())}.zip`;
  const zipPath = join(outDir, zipName);
  const zipBuffer = buildZip(entries, new Date());
  writeFileSync(zipPath, zipBuffer);
  const removedPrevious = removePreviousPackages(outDir, key, zipPath);

  return {
    ok: true,
    key,
    zip: zipPath,
    zipBytes: zipBuffer.length,
    removedPrevious,
    entries: entries.map((entry) => ({ name: entry.name, bytes: entry.data.length })),
    warnings,
  };
}

/** 解析 --key value 参数；--extra 可重复。 */
function parseArgs(argv) {
  const options = {};
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (!item.startsWith("--")) continue;
    const name = item.slice(2);
    const next = argv[index + 1];
    if (next === undefined || next.startsWith("--")) {
      options[name] = true;
      continue;
    }
    if (name === "extra") options.extra = [].concat(options.extra || [], next);
    else options[name] = next;
    index += 1;
  }
  return options;
}

function main() {
  const options = parseArgs(process.argv.slice(2));
  if (!options.key || !options.comment || (!options.pdca && options["no-pdca"] !== true)) {
    throw new PackError("用法：--key <KEY> --comment <草稿> --pdca <报告md>|--no-pdca [--extra <附加文件>] [--out <目录>]", "missing-argument");
  }
  process.stdout.write(`${JSON.stringify(pack(options), null, 2)}\n`);
}

try {
  main();
} catch (error) {
  const reason = error && error.reason ? `（${error.reason}）` : "";
  process.stderr.write(`[打包失败]${reason} ${(error && error.message) || error}\n`);
  process.exitCode = 1;
}

