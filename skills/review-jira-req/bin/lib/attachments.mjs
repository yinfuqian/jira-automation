/**
 * 附件下载与文本抽取（零第三方依赖）。
 *
 * 设计约束：
 * - 对 JIRA 只读：这里只发 GET，且仅在目标 URL 通过同源校验后才附加 PAT。
 * - 下载与抽取分离：原始字节落盘，抽取文本写成同名 `.txt` 兄弟文件；
 *   raw 清单只记录状态与路径，不内嵌正文。
 * - 附件内容是不可信数据。本模块只负责解码成文本，不做任何解释或执行。
 * - 单附件失败只记录状态，不中断其余附件，也不中断整轮评审。
 */

import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFile, mkdir, rm, writeFile } from 'node:fs/promises';
import { basename, extname, join, relative, resolve, sep } from 'node:path';
import { promisify } from 'node:util';
import { inflateRawSync } from 'node:zlib';

const execFileAsync = promisify(execFile);

export const DEFAULT_ATTACHMENT_LIMITS = {
  maxAttachments: 10,
  maxFileBytes: 5 * 1024 * 1024,
  maxTotalBytes: 15 * 1024 * 1024,
};

/** 抽取文本的上下文预算：超出即截断，避免把上下文塞爆。 */
export const DEFAULT_TEXT_LIMITS = {
  perFileChars: 30000,
  perIssueChars: 80000,
};

/** ZIP 解压防炸弹上限。 */
export const ZIP_LIMITS = {
  maxEntries: 2000,
  maxEntryBytes: 20 * 1024 * 1024,
  maxTotalBytes: 50 * 1024 * 1024,
  maxRatio: 100,
};

export class ZipError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ZipError';
  }
}

// ---------------------------------------------------------------------------
// 文件名与路径
// ---------------------------------------------------------------------------

/** 只保留安全字符；任何分隔符、控制字符与 Windows 保留字符都被替换。 */
export function sanitizeFilename(name) {
  const raw = String(name ?? '').replace(/\\/g, '/');
  let safe = basename(raw)
    .replace(/[\u0000-\u001f\u007f]/g, '_')
    .replace(/[<>:"/\\|?*]/g, '_')
    .replace(/\s+/g, ' ')
    .trim();

  // 去掉纯点段（. / ..），避免落盘成隐藏文件或目录跳转。
  if (/^\.+$/.test(safe)) safe = '';

  if (safe.length > 120) {
    const ext = extname(safe).slice(0, 20);
    safe = safe.slice(0, Math.max(1, 120 - ext.length)) + ext;
  }

  return safe || 'attachment';
}

function sanitizeId(id) {
  return String(id ?? '').replace(/[^A-Za-z0-9._-]/g, '_') || 'attachment';
}

function sanitizeKey(key) {
  return String(key ?? '').replace(/[^A-Za-z0-9._-]/g, '_');
}

/** 校验落盘路径没有离开附件目录（防路径穿越）。 */
export function assertWithinDir(dir, file) {
  const resolvedDir = resolve(dir);
  const resolvedFile = resolve(file);
  if (resolvedFile !== resolvedDir && !resolvedFile.startsWith(`${resolvedDir}${sep}`)) {
    throw new Error('附件落盘路径越界，已拒绝写入');
  }
  return resolvedFile;
}

function toPosixRelative(root, file) {
  return relative(root, file).split(sep).join('/');
}

// ---------------------------------------------------------------------------
// HTML / XML 文本处理
// ---------------------------------------------------------------------------

const XML_ENTITY_NAMES = {
  lt: '<',
  gt: '>',
  amp: '&',
  quot: '"',
  apos: "'",
};

function decodeEntities(text, extra = {}) {
  return String(text).replace(/&(#x[0-9a-f]+|#\d+|[a-z][a-z0-9]+);/gi, (match, body) => {
    const lower = body.toLowerCase();
    if (lower.startsWith('#x')) {
      const code = Number.parseInt(lower.slice(2), 16);
      return Number.isFinite(code) ? String.fromCodePoint(code) : match;
    }
    if (lower.startsWith('#')) {
      const code = Number.parseInt(lower.slice(1), 10);
      return Number.isFinite(code) ? String.fromCodePoint(code) : match;
    }
    if (Object.hasOwn(extra, lower)) return extra[lower];
    return Object.hasOwn(XML_ENTITY_NAMES, lower) ? XML_ENTITY_NAMES[lower] : match;
  });
}

function stripTags(xml) {
  return decodeEntities(String(xml).replace(/<[^>]*>/g, ''));
}

function tidyText(text) {
  return String(text)
    .replace(/\r\n?/g, '\n')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

/** HTML → 纯文本：先剥脚本/样式，再按块级标签断行并解码实体。 */
export function htmlToText(html) {
  let text = String(html);
  text = text.replace(/<!--[\s\S]*?-->/g, ' ');
  text = text.replace(/<(script|style|noscript|template)\b[^>]*>[\s\S]*?<\/\1>/gi, ' ');
  text = text.replace(/<br\s*\/?>/gi, '\n');
  text = text.replace(/<\/(p|div|li|h[1-6]|tr|section|article|table|thead|tbody|ul|ol|pre|blockquote)>/gi, '\n');
  text = text.replace(/<[^>]+>/g, ' ');
  text = decodeEntities(text, { nbsp: ' ' });
  return tidyText(text);
}

// ---------------------------------------------------------------------------
// 最小 ZIP 读取器（docx / xlsx / pptx）
// ---------------------------------------------------------------------------

const ZIP_EOCD_SIG = 0x06054b50;
const ZIP_CENTRAL_SIG = 0x02014b50;
const ZIP_LOCAL_SIG = 0x04034b50;

function assertSafeEntryName(name) {
  if (!name || name.includes('\0')) throw new ZipError('ZIP 条目名为空或含空字符');
  const normalized = name.replace(/\\/g, '/');
  if (normalized.startsWith('/') || /^[A-Za-z]:/.test(normalized)) {
    throw new ZipError(`ZIP 条目使用绝对路径：${name}`);
  }
  if (normalized.split('/').some((segment) => segment === '..')) {
    throw new ZipError(`ZIP 条目路径越界：${name}`);
  }
}

function findEocd(buffer) {
  const minimum = Math.max(0, buffer.length - 22 - 0xffff);
  for (let offset = buffer.length - 22; offset >= minimum; offset -= 1) {
    if (buffer.readUInt32LE(offset) === ZIP_EOCD_SIG) return offset;
  }
  throw new ZipError('不是有效的 ZIP：找不到中央目录结尾');
}

/**
 * 解析 ZIP 中央目录，返回 name → 条目元数据。
 * 只记录元数据，正文按需读取；所有上限在读取正文前先检查。
 */
export function readZip(buffer, limits = ZIP_LIMITS) {
  const buf = Buffer.isBuffer(buffer) ? buffer : Buffer.from(buffer);
  const eocd = findEocd(buf);

  const entryCount = buf.readUInt16LE(eocd + 10);
  const centralSize = buf.readUInt32LE(eocd + 12);
  const centralOffset = buf.readUInt32LE(eocd + 16);

  if (entryCount === 0xffff || centralSize === 0xffffffff || centralOffset === 0xffffffff) {
    throw new ZipError('不支持 ZIP64 格式');
  }
  if (entryCount > limits.maxEntries) {
    throw new ZipError(`ZIP 条目数超过上限（${entryCount} > ${limits.maxEntries}）`);
  }
  if (centralOffset + centralSize > buf.length) {
    throw new ZipError('ZIP 中央目录越界');
  }

  const entries = new Map();
  let offset = centralOffset;
  let totalUncompressed = 0;

  for (let index = 0; index < entryCount; index += 1) {
    if (offset + 46 > buf.length || buf.readUInt32LE(offset) !== ZIP_CENTRAL_SIG) {
      throw new ZipError('ZIP 中央目录损坏');
    }

    const flags = buf.readUInt16LE(offset + 8);
    const method = buf.readUInt16LE(offset + 10);
    const compressedSize = buf.readUInt32LE(offset + 20);
    const uncompressedSize = buf.readUInt32LE(offset + 24);
    const nameLength = buf.readUInt16LE(offset + 28);
    const extraLength = buf.readUInt16LE(offset + 30);
    const commentLength = buf.readUInt16LE(offset + 32);
    const localOffset = buf.readUInt32LE(offset + 42);

    if (offset + 46 + nameLength > buf.length) throw new ZipError('ZIP 条目名越界');
    const name = buf.toString('utf8', offset + 46, offset + 46 + nameLength);

    if (flags & 0x0001) throw new ZipError(`不支持加密 ZIP 条目：${name}`);
    if (method !== 0 && method !== 8) throw new ZipError(`不支持的 ZIP 压缩方法 ${method}：${name}`);
    if (
      compressedSize === 0xffffffff ||
      uncompressedSize === 0xffffffff ||
      localOffset === 0xffffffff
    ) {
      throw new ZipError('不支持 ZIP64 条目');
    }
    assertSafeEntryName(name);

    if (uncompressedSize > limits.maxEntryBytes) {
      throw new ZipError(`ZIP 条目解压后超过上限：${name}`);
    }
    if (compressedSize === 0) {
      if (uncompressedSize > 0) throw new ZipError(`ZIP 条目压缩比异常：${name}`);
    } else if (uncompressedSize / compressedSize > limits.maxRatio) {
      throw new ZipError(`ZIP 条目压缩比异常：${name}`);
    }
    totalUncompressed += uncompressedSize;
    if (totalUncompressed > limits.maxTotalBytes) {
      throw new ZipError('ZIP 总解压大小超过上限');
    }

    if (entries.has(name)) throw new ZipError(`ZIP 条目重复：${name}`);
    entries.set(name, {
      name,
      method,
      compressedSize,
      uncompressedSize,
      localOffset,
      flags,
    });

    offset += 46 + nameLength + extraLength + commentLength;
  }

  return entries;
}

/** 读取单个条目并解压。 */
export function readZipEntry(buffer, entry, limits = ZIP_LIMITS) {
  const buf = Buffer.isBuffer(buffer) ? buffer : Buffer.from(buffer);
  const local = entry.localOffset;
  if (local + 30 > buf.length || buf.readUInt32LE(local) !== ZIP_LOCAL_SIG) {
    throw new ZipError(`ZIP 本地文件头损坏：${entry.name}`);
  }

  const nameLength = buf.readUInt16LE(local + 26);
  const extraLength = buf.readUInt16LE(local + 28);
  const dataStart = local + 30 + nameLength + extraLength;
  const dataEnd = dataStart + entry.compressedSize;
  if (dataEnd > buf.length) throw new ZipError(`ZIP 条目数据越界：${entry.name}`);

  const data = buf.subarray(dataStart, dataEnd);

  if (entry.method === 0) {
    if (data.length !== entry.uncompressedSize) {
      throw new ZipError(`ZIP 条目大小与声明不符：${entry.name}`);
    }
    return Buffer.from(data);
  }

  let output;
  try {
    output = inflateRawSync(data, { maxOutputLength: limits.maxEntryBytes });
  } catch (error) {
    throw new ZipError(`ZIP 条目解压失败：${entry.name}（${error.message}）`);
  }
  if (output.length !== entry.uncompressedSize) {
    throw new ZipError(`ZIP 条目解压大小与声明不符：${entry.name}`);
  }
  return output;
}

function readZipText(buffer, name) {
  const entries = readZip(buffer);
  const entry = entries.get(name);
  if (!entry) throw new ZipError(`缺少 ZIP 条目：${name}`);
  return readZipEntry(buffer, entry).toString('utf8');
}

// ---------------------------------------------------------------------------
// Office 文档抽取
// ---------------------------------------------------------------------------

/** docx：保留段落、换行与制表符。 */
export function extractDocxText(buffer) {
  let xml = readZipText(buffer, 'word/document.xml');
  xml = xml
    .replace(/<w:tab\b[^>]*\/>/g, '\t')
    .replace(/<w:br\b[^>]*\/>/g, '\n')
    .replace(/<\/w:tc>/g, '\t')
    .replace(/<\/w:p>/g, '\n')
    .replace(/<\/w:tr>/g, '\n');
  return tidyText(stripTags(xml));
}

function collectTextRuns(fragment) {
  let text = '';
  for (const match of String(fragment).matchAll(/<t\b[^>]*>([\s\S]*?)<\/t>/g)) {
    text += decodeEntities(match[1]);
  }
  return text;
}

function readSharedStrings(buffer) {
  const entries = readZip(buffer);
  const entry = entries.get('xl/sharedStrings.xml');
  if (!entry) return [];
  const xml = readZipEntry(buffer, entry).toString('utf8');
  const values = [];
  for (const match of xml.matchAll(/<si\b[^>]*>([\s\S]*?)<\/si>/g)) {
    values.push(collectTextRuns(match[1]));
  }
  return values;
}

/** xlsx：共享字符串 + 各工作表单元格（含 inlineStr 与富文本）。 */
export function extractXlsxText(buffer) {
  const shared = readSharedStrings(buffer);
  const entries = readZip(buffer);
  const sheetNames = [...entries.keys()]
    .filter((name) => /^xl\/worksheets\/sheet\d+\.xml$/.test(name))
    .sort((a, b) => {
      const na = Number(a.match(/sheet(\d+)\.xml$/)[1]);
      const nb = Number(b.match(/sheet(\d+)\.xml$/)[1]);
      return na - nb;
    });

  const lines = [];
  for (const name of sheetNames) {
    const xml = readZipEntry(buffer, entries.get(name)).toString('utf8');
    for (const rowMatch of xml.matchAll(/<row\b[^>]*>([\s\S]*?)<\/row>/g)) {
      const cells = [];
      for (const cellMatch of rowMatch[1].matchAll(/<c\b([^>]*)>([\s\S]*?)<\/c>/g)) {
        const attrs = cellMatch[1];
        const inner = cellMatch[2];
        const type = (attrs.match(/\bt="([^"]+)"/) ?? [])[1] ?? '';
        let value = '';

        if (type === 'inlineStr') {
          value = collectTextRuns(inner);
        } else {
          const v = (inner.match(/<v\b[^>]*>([\s\S]*?)<\/v>/) ?? [])[1];
          if (v !== undefined) {
            value = decodeEntities(v);
            if (type === 's') {
              const index = Number.parseInt(value, 10);
              value = Number.isInteger(index) && shared[index] !== undefined ? shared[index] : '';
            }
          } else {
            value = collectTextRuns(inner);
          }
        }
        cells.push(value);
      }
      if (cells.some((cell) => cell !== '')) lines.push(cells.join('\t'));
    }
    if (lines.length > 0) lines.push('');
  }

  return tidyText(lines.join('\n'));
}

/** pptx：按页提取 `<a:t>` 文本。 */
export function extractPptxText(buffer) {
  const entries = readZip(buffer);
  const slideNames = [...entries.keys()]
    .filter((name) => /^ppt\/slides\/slide\d+\.xml$/.test(name))
    .sort((a, b) => {
      const na = Number(a.match(/slide(\d+)\.xml$/)[1]);
      const nb = Number(b.match(/slide(\d+)\.xml$/)[1]);
      return na - nb;
    });

  const pages = [];
  for (const name of slideNames) {
    const xml = readZipEntry(buffer, entries.get(name)).toString('utf8');
    const texts = [];
    for (const match of xml.matchAll(/<a:t\b[^>]*>([\s\S]*?)<\/a:t>/g)) {
      texts.push(decodeEntities(match[1]));
    }
    if (texts.length > 0) pages.push(texts.join('\n'));
  }

  return tidyText(pages.join('\n\n'));
}

// ---------------------------------------------------------------------------
// 格式识别与文本抽取
// ---------------------------------------------------------------------------

const TEXT_EXTENSIONS = new Set([
  'txt', 'md', 'markdown', 'csv', 'json', 'xml', 'yaml', 'yml', 'log', 'sql',
]);
const HTML_EXTENSIONS = new Set(['html', 'htm']);
const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg']);

function extensionOf(filename) {
  return extname(String(filename ?? '')).toLowerCase().replace(/^\./, '');
}

export function classifyAttachment(filename, mimeType = '') {
  const ext = extensionOf(filename);
  const mime = String(mimeType ?? '').toLowerCase();

  if (TEXT_EXTENSIONS.has(ext)) return 'text';
  if (HTML_EXTENSIONS.has(ext)) return 'html';
  if (ext === 'docx') return 'docx';
  if (ext === 'xlsx') return 'xlsx';
  if (ext === 'pptx') return 'pptx';
  if (ext === 'pdf') return 'pdf';
  if (ext === 'doc') return 'doc';
  if (ext === 'xls') return 'xls';
  if (IMAGE_EXTENSIONS.has(ext)) return 'image';

  // 没有扩展名时按 MIME 兜底。
  if (mime.startsWith('image/')) return 'image';
  if (mime === 'application/pdf') return 'pdf';
  if (mime.includes('wordprocessingml')) return 'docx';
  if (mime.includes('spreadsheetml')) return 'xlsx';
  if (mime.includes('presentationml')) return 'pptx';
  if (mime.startsWith('text/') || mime === 'application/json' || mime === 'application/xml') {
    return 'text';
  }
  return 'unsupported';
}

async function defaultRunCommand(command, args, options = {}) {
  const { stdout } = await execFileAsync(command, args, { maxBuffer: 20 * 1024 * 1024, ...options });
  return stdout;
}

/**
 * 抽取单个附件的文本。
 * @returns {Promise<{kind: 'text'|'image'|'unsupported', text?: string, note?: string}>}
 */
export async function extractAttachment({
  filePath,
  filename = basename(filePath),
  mimeType = '',
  runCommand = defaultRunCommand,
} = {}) {
  const kind = classifyAttachment(filename, mimeType);

  if (kind === 'image') {
    return { kind: 'image', note: '图片附件，需由模型直接查看' };
  }

  if (kind === 'text') {
    const text = (await readFile(filePath, 'utf8')).replace(/^\uFEFF/, '');
    return { kind: 'text', text: tidyText(text) };
  }

  if (kind === 'html') {
    return { kind: 'text', text: htmlToText(await readFile(filePath, 'utf8')) };
  }

  if (kind === 'docx' || kind === 'xlsx' || kind === 'pptx') {
    const buffer = await readFile(filePath);
    const text =
      kind === 'docx'
        ? extractDocxText(buffer)
        : kind === 'xlsx'
          ? extractXlsxText(buffer)
          : extractPptxText(buffer);
    if (!text) {
      return { kind: 'unsupported', note: '需人工查看：未能从该 Office 文件中抽取到文本' };
    }
    return { kind: 'text', text };
  }

  if (kind === 'pdf') {
    try {
      const text = tidyText(await runCommand('pdftotext', ['-layout', '-enc', 'UTF-8', filePath, '-']));
      if (!text) {
        return { kind: 'unsupported', note: '需人工查看：PDF 未抽取到文本（可能是扫描件）' };
      }
      return { kind: 'text', text };
    } catch {
      return { kind: 'unsupported', note: '需人工查看：本机无法调用 pdftotext 抽取 PDF 文本' };
    }
  }

  if (kind === 'doc') {
    try {
      const text = tidyText(await runCommand('textutil', ['-convert', 'txt', '-stdout', filePath]));
      if (!text) {
        return { kind: 'unsupported', note: '需人工查看：未能从该 .doc 文件中抽取到文本' };
      }
      return { kind: 'text', text };
    } catch {
      return { kind: 'unsupported', note: '需人工查看：本机无法调用 textutil 转换旧版 .doc' };
    }
  }

  if (kind === 'xls') {
    return { kind: 'unsupported', note: '需人工查看：旧版 .xls 无可用解码器' };
  }

  return { kind: 'unsupported', note: '需人工查看：不支持的附件类型' };
}

// ---------------------------------------------------------------------------
// 下载
// ---------------------------------------------------------------------------

export function emptyAttachmentStats() {
  return {
    total: 0,
    text: 0,
    image: 0,
    unsupported: 0,
    skipped: 0,
    failed: 0,
    uninspected: 0,
  };
}

export function summarizeAttachments(attachments = []) {
  const stats = emptyAttachmentStats();
  stats.total = attachments.length;
  for (const attachment of attachments) {
    const status = attachment?.status;
    if (Object.hasOwn(stats, status) && status !== 'total' && status !== 'uninspected') {
      stats[status] += 1;
    }
  }
  stats.uninspected = stats.unsupported + stats.skipped + stats.failed;
  return stats;
}

export function addAttachmentStats(left, right) {
  const result = emptyAttachmentStats();
  for (const key of Object.keys(result)) {
    result[key] = (left?.[key] ?? 0) + (right?.[key] ?? 0);
  }
  return result;
}

/**
 * 校验并解析附件下载地址。
 * 只允许 http(s)、同源、无内嵌凭据，且不允许 https → http 降级。
 * 错误信息只描述原因，不回显完整 URL。
 */
export function resolveAttachmentUrl(content, baseUrl) {
  if (typeof content !== 'string' || content.trim() === '') {
    throw new Error('附件缺少下载地址');
  }

  const base = new URL(baseUrl);
  let target;
  try {
    target = new URL(content, `${base.origin}/`);
  } catch {
    throw new Error('附件下载地址不合法');
  }

  if (target.protocol !== 'http:' && target.protocol !== 'https:') {
    throw new Error('附件下载地址协议不受支持');
  }
  if (target.username || target.password) {
    throw new Error('附件下载地址内嵌凭据，已拒绝');
  }
  if (base.protocol === 'https:' && target.protocol === 'http:') {
    throw new Error('附件下载地址发生 https → http 降级，已拒绝');
  }
  if (target.origin !== base.origin) {
    throw new Error('附件下载地址跨源，已拒绝');
  }

  return target;
}

async function readResponseBody(response, maxBytes) {
  if (!response.body || typeof response.body.getReader !== 'function') {
    const buffer = Buffer.from(await response.arrayBuffer());
    return buffer.length > maxBytes
      ? { tooLarge: true, total: buffer.length }
      : { buffer, total: buffer.length };
  }

  const reader = response.body.getReader();
  const chunks = [];
  let total = 0;

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > maxBytes) {
      try {
        await reader.cancel();
      } catch {
        // 取消失败不影响结论
      }
      return { tooLarge: true, total };
    }
    chunks.push(Buffer.from(value));
  }

  return { buffer: Buffer.concat(chunks), total };
}

/**
 * 下载并抽取一条 issue 的全部附件。
 *
 * @returns {Promise<{attachments: object[], stats: object}>}
 */
export async function downloadAttachments({
  attachments = [],
  baseUrl,
  pat,
  key,
  runDir,
  limits = DEFAULT_ATTACHMENT_LIMITS,
  textLimits = DEFAULT_TEXT_LIMITS,
  fetchImpl = fetch,
  runCommand = defaultRunCommand,
  logger,
  enabled = true,
  dedupe = true,
  timeoutMs = 30000,
} = {}) {
  if (!enabled) {
    return { attachments: [], stats: emptyAttachmentStats() };
  }

  const attachDir = join(runDir, 'attachments', sanitizeKey(key));
  // 同一 run-id 复跑时先清空，避免旧文件冒充本次结果。
  // 即使本次没有附件也要清，否则上一轮留下的文件会让清单与目录对不上。
  await rm(attachDir, { recursive: true, force: true });
  if (!Array.isArray(attachments) || attachments.length === 0) {
    return { attachments: [], stats: emptyAttachmentStats() };
  }
  await mkdir(attachDir, { recursive: true });

  const results = [];
  let totalBytes = 0;
  let usedChars = 0;
  /** 内容摘要 → 首个落盘的附件，用于同一 issue 内的去重。 */
  const seenDigests = new Map();

  for (const [index, raw] of attachments.entries()) {
    const id = raw?.id !== undefined && raw?.id !== null ? String(raw.id) : '';
    const filename = typeof raw?.filename === 'string' ? raw.filename : '(未命名附件)';
    const mimeType = typeof raw?.mimeType === 'string' ? raw.mimeType : '';
    const declaredSize = Number(raw?.size);
    const size = Number.isFinite(declaredSize) && declaredSize >= 0 ? declaredSize : 0;

    const entry = {
      id: id || `attachment-${index + 1}`,
      filename,
      mimeType,
      size,
      status: 'skipped',
      localPath: null,
      textPath: null,
      textChars: 0,
      truncated: false,
      note: '',
    };

    const skip = (note) => {
      entry.note = note;
      results.push(entry);
      logger?.warn(`${key} 附件 ${entry.filename} 已跳过：${note}`);
    };

    const fail = (note) => {
      entry.status = 'failed';
      entry.note = note;
      results.push(entry);
      logger?.warn(`${key} 附件 ${entry.filename} 处理失败：${note}`);
    };

    if (!id) {
      fail('附件缺少 id');
      continue;
    }
    if (index >= limits.maxAttachments) {
      skip(`超出单条 issue 附件数量上限（${limits.maxAttachments} 个）`);
      continue;
    }
    if (size > limits.maxFileBytes) {
      skip(`超过单文件大小上限（${formatBytes(limits.maxFileBytes)}）`);
      continue;
    }
    if (totalBytes + size > limits.maxTotalBytes) {
      skip(`超过单条 issue 附件总大小上限（${formatBytes(limits.maxTotalBytes)}）`);
      continue;
    }

    let target;
    try {
      target = resolveAttachmentUrl(raw?.content, baseUrl);
    } catch (error) {
      fail(error.message);
      continue;
    }

    let response;
    try {
      response = await fetchImpl(target, {
        method: 'GET',
        redirect: 'manual',
        headers: { Authorization: `Bearer ${pat}` },
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch {
      // 不把底层错误信息拼进来：其中可能带完整 URL。
      fail('下载请求失败（网络错误或超时）');
      continue;
    }

    if (response.status >= 300 && response.status < 400) {
      fail('附件下载发生重定向，已拒绝跟随');
      continue;
    }
    if (!response.ok) {
      fail(`附件下载失败（HTTP ${response.status}）`);
      continue;
    }

    const contentLength = Number(response.headers?.get?.('content-length'));
    if (Number.isFinite(contentLength) && contentLength > limits.maxFileBytes) {
      fail(`实际大小超过单文件上限（${formatBytes(limits.maxFileBytes)}）`);
      continue;
    }
    if (Number.isFinite(contentLength) && totalBytes + contentLength > limits.maxTotalBytes) {
      fail(`实际总大小超过单条 issue 上限（${formatBytes(limits.maxTotalBytes)}）`);
      continue;
    }

    let body;
    try {
      body = await readResponseBody(response, Math.min(limits.maxFileBytes, limits.maxTotalBytes - totalBytes));
    } catch {
      fail('读取附件内容失败');
      continue;
    }
    if (body.tooLarge) {
      fail(`实际大小超过上限（${formatBytes(limits.maxFileBytes)}）`);
      continue;
    }
    if (totalBytes + body.total > limits.maxTotalBytes) {
      fail(`实际总大小超过单条 issue 上限（${formatBytes(limits.maxTotalBytes)}）`);
      continue;
    }

    // 按内容去重：同一 issue 内相同内容的附件只落盘、只抽取一次。
    // 重复附件仍计入 totalBytes（确实下载了），但不占磁盘、不占文本预算。
    const digest = dedupe ? createHash('sha256').update(body.buffer).digest('hex') : null;
    const previous = digest ? seenDigests.get(digest) : null;
    if (previous) {
      totalBytes += body.total;
      entry.status = 'skipped';
      entry.note = `重复附件：与附件 ${previous.id}（${previous.filename}）内容相同，未重复落盘`;
      results.push(entry);
      logger?.info(`${key} 附件 ${filename} 与附件 ${previous.id} 内容相同，已去重`);
      continue;
    }

    const safeName = sanitizeFilename(filename);
    const storedName = `${sanitizeId(id)}-${safeName}`;
    let localPath;
    try {
      localPath = assertWithinDir(attachDir, join(attachDir, storedName));
      await writeFile(localPath, body.buffer);
    } catch (error) {
      fail(`附件落盘失败：${error.message}`);
      continue;
    }

    totalBytes += body.total;
    entry.localPath = toPosixRelative(runDir, localPath);
    if (digest) seenDigests.set(digest, { id: entry.id, filename });

    let extracted;
    try {
      extracted = await extractAttachment({ filePath: localPath, filename, mimeType, runCommand });
    } catch (error) {
      entry.status = 'failed';
      entry.note = `抽取失败：${error.message}`;
      results.push(entry);
      logger?.warn(`${key} 附件 ${filename} 抽取失败：${error.message}`);
      continue;
    }

    if (extracted.kind === 'image') {
      entry.status = 'image';
      entry.note = extracted.note ?? '';
      results.push(entry);
      continue;
    }

    if (extracted.kind !== 'text') {
      entry.status = 'unsupported';
      entry.note = extracted.note ?? '需人工查看：不支持的附件类型';
      results.push(entry);
      continue;
    }

    const text = extracted.text ?? '';
    const remaining = Math.max(0, textLimits.perIssueChars - usedChars);
    const cap = Math.min(textLimits.perFileChars, remaining);
    const kept = text.slice(0, cap);

    if (!kept) {
      entry.status = 'unsupported';
      entry.note =
        text.trim() === ''
          ? '需人工查看：未抽取到文本'
          : `需人工查看：超出单 issue 文本抽取上限（${textLimits.perIssueChars} 字符）`;
      results.push(entry);
      continue;
    }

    const textPath = `${localPath}.txt`;
    try {
      await writeFile(textPath, kept, 'utf8');
    } catch (error) {
      entry.status = 'failed';
      entry.note = `抽取文本落盘失败：${error.message}`;
      results.push(entry);
      continue;
    }

    usedChars += kept.length;
    entry.status = 'text';
    entry.textPath = toPosixRelative(runDir, textPath);
    entry.textChars = kept.length;
    entry.truncated = kept.length < text.length;
    const notes = [];
    if (extracted.note) notes.push(extracted.note);
    if (entry.truncated) notes.push(`已截断（原文 ${text.length} 字符）`);
    entry.note = notes.join('；');
    results.push(entry);
  }

  return { attachments: results, stats: summarizeAttachments(results) };
}

export function formatBytes(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) return '(未知)';
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}
