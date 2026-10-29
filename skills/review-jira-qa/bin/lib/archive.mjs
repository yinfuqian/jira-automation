/**
 * 压缩包读取：列条目、按白名单筛选、只解选中项。
 *
 * - .rar / .7z / .zip 统一走包内 libarchive-wasm；
 * - 只在内存中读取选中条目，不落盘（落盘由调用方决定）；
 * - 加密、损坏、超限 → 返回 unreadable/truncated，不抛未捕获异常。
 */

import { createHash } from 'node:crypto';
import path from 'node:path';

import { createArchiveReader, loadLibarchiveVendor } from './archive-wasm.mjs';
import {
  basenameOf,
  classifyRole,
  extensionOf,
  isArchiveMemberExtension,
  isExcludedRole,
  isIgnoredPath,
} from './classify.mjs';
import { EXIT, QaError } from './env.mjs';

const MAGIC = {
  zip: ['504b0304', '504b0506', '504b0708'],
  rar: ['526172211a0700', '526172211a070100'],
  '7z': ['377abcaf271c'],
};

export function archiveLimits(env = process.env) {
  const num = (name, fallback) => {
    const value = Number(env[name]);
    return Number.isFinite(value) && value > 0 ? value : fallback;
  };
  return {
    maxArchiveBytes: num('QA_MAX_ARCHIVE_MB', 100) * 1024 * 1024,
    maxEntries: num('QA_MAX_ARCHIVE_ENTRIES', 5000),
    maxSelected: num('QA_MAX_ARCHIVE_SELECTED', 200),
    maxFileBytes: num('QA_MAX_ARCHIVE_FILE_MB', 64) * 1024 * 1024,
    maxTotalBytes: num('QA_MAX_ARCHIVE_TOTAL_MB', 200) * 1024 * 1024,
    maxDepth: num('QA_ARCHIVE_DEPTH', 2),
  };
}

export function detectArchiveFormat(filename, buffer) {
  if (Buffer.isBuffer(buffer) && buffer.length >= 4) {
    const head = buffer.subarray(0, 8).toString('hex');
    for (const [format, magics] of Object.entries(MAGIC)) {
      if (magics.some((magic) => head.startsWith(magic))) return format;
    }
  }
  const ext = extensionOf(filename);
  if (ext === '.zip') return 'zip';
  if (ext === '.rar') return 'rar';
  if (ext === '.7z') return '7z';
  return '';
}

function shortenPath(value) {
  if (value.length <= 240) return value;
  const hash = createHash('sha1').update(value).digest('hex').slice(0, 10);
  const ext = path.posix.extname(value);
  return `${value.slice(0, 200)}-${hash}${ext}`;
}

export function sanitizeEntryPath(raw) {
  let value = String(raw ?? '').replace(/\\/g, '/').replace(/^\.\/+/, '').trim();
  if (!value || value.endsWith('/')) return '';
  if (value.startsWith('/') || /^[A-Za-z]:/.test(value)) return '';
  const segments = value.split('/');
  if (segments.some((segment) => !segment || segment === '.' || segment === '..')) return '';
  const safe = segments
    .map((segment) =>
      segment
        .replace(/[<>:"|?*\u0000-\u001f]/g, '_')
        .replace(/\s+/g, ' ')
        .trim()
        .replace(/[. ]+$/g, ''),
    )
    .filter(Boolean);
  if (safe.length === 0) return '';
  return shortenPath(safe.join('/'));
}

export function ignoreReason(entryPath) {
  if (!entryPath) return 'path';
  if (isIgnoredPath(entryPath)) return 'ignored-dir';
  if (!isArchiveMemberExtension(entryPath)) return 'extension';
  const role = classifyRole(basenameOf(entryPath), entryPath);
  if (isExcludedRole(role)) return 'role';
  return '';
}

/**
 * 读取压缩包并只解出白名单内的需求材料。
 *
 * 返回：
 * {
 *   ok, format, entryCount, selected: [{path,size,role,data}], ignored: {..},
 *   truncated, unreadable, encrypted, reason, limits
 * }
 */
export async function inspectArchive(buffer, filename, { env = process.env } = {}) {
  const limits = archiveLimits(env);
  const format = detectArchiveFormat(filename, buffer);
  if (!format) {
    return {
      ok: false,
      format: '',
      entryCount: 0,
      selected: [],
      ignored: {},
      truncated: false,
      unreadable: true,
      encrypted: false,
      reason: '不是受支持的压缩包格式（zip/rar/7z）',
      limits,
    };
  }
  if (buffer.length > limits.maxArchiveBytes) {
    return {
      ok: false,
      format,
      entryCount: 0,
      selected: [],
      ignored: {},
      truncated: false,
      unreadable: true,
      encrypted: false,
      reason: `压缩包超过 ${Math.round(limits.maxArchiveBytes / 1024 / 1024)}MB 上限`,
      limits,
    };
  }

  const ignored = { path: 0, 'ignored-dir': 0, extension: 0, role: 0, size: 0, count: 0 };
  const selected = [];
  let entryCount = 0;
  let candidateCount = 0;
  let totalBytes = 0;
  let truncated = false;
  let unreadable = false;
  let encrypted = false;
  let reason = '';

  await loadLibarchiveVendor();
  const { reader } = await createArchiveReader(buffer);
  try {
    encrypted = Boolean(reader.hasEncryptedData && reader.hasEncryptedData());
    for (const entry of reader.entries()) {
      entryCount += 1;
      const rawPath = entry.getPathname();
      const size = Number(entry.getSize() || 0);
      const clean = sanitizeEntryPath(rawPath);
      if (!clean) {
        ignored.path += 1;
        continue;
      }
      const why = ignoreReason(clean);
      if (why) {
        ignored[why] = (ignored[why] || 0) + 1;
        continue;
      }
      candidateCount += 1;
      if (candidateCount > limits.maxEntries) {
        truncated = true;
        ignored.count += 1;
        continue;
      }
      if (selected.length >= limits.maxSelected) {
        truncated = true;
        ignored.count += 1;
        continue;
      }
      if (size > limits.maxFileBytes) {
        truncated = true;
        ignored.size += 1;
        continue;
      }
      if (totalBytes + size > limits.maxTotalBytes) {
        truncated = true;
        ignored.size += 1;
        continue;
      }
      let data = Buffer.alloc(0);
      if (size > 0) {
        const raw = entry.readData();
        data = raw ? Buffer.from(raw) : Buffer.alloc(0);
      }
      totalBytes += data.length;
      selected.push({ path: clean, size: data.length, role: classifyRole(basenameOf(clean), clean), data });
    }
  } catch (error) {
    unreadable = true;
    reason = error?.message || String(error);
  } finally {
    try {
      reader.free();
    } catch {
      /* ignore */
    }
  }

  if (encrypted && selected.length === 0) {
    unreadable = true;
    reason = '压缩包加密，无法读取';
  }

  return {
    ok: true,
    format,
    entryCount,
    selected,
    ignored,
    truncated,
    unreadable,
    encrypted,
    reason,
    limits,
  };
}

export function nestedArchiveEntries(selected, { depth, maxDepth }) {
  if (depth >= maxDepth) return [];
  return selected.filter((item) => item.data && ['.zip', '.rar', '.7z'].includes(extensionOf(item.path)));
}

export function requireArchiveSupport() {
  return loadLibarchiveVendor().then(() => true).catch(() => false);
}

export function archiveUnavailableError(reason) {
  return new QaError(`压缩包不可读：${reason}`, EXIT.VALIDATION);
}
