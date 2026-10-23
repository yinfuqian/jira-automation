/**
 * 环境、参数、工作区与通用错误处理。
 *
 * 硬约束：
 * - Jira 凭据只从环境变量读取，不接受命令行传入，不写入任何文件。
 * - 产物只能落在调用者工作区，不能落进技能安装目录。
 */

import { existsSync } from 'node:fs';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const EXIT = {
  OK: 0,
  INTERNAL: 1,
  USAGE: 2,
  AUTH: 3,
  NOT_FOUND: 4,
  NETWORK: 5,
  VALIDATION: 6,
  PRECONDITION: 7,
  REMOTE_WRITE: 8,
};

export class QaError extends Error {
  constructor(message, code = EXIT.INTERNAL, details = undefined) {
    super(message);
    this.name = 'QaError';
    this.code = code;
    this.details = details;
  }
}

export function fail(message, code = EXIT.INTERNAL, details) {
  throw new QaError(message, code, details);
}

export function checkNode() {
  const major = Number(process.versions.node.split('.')[0]);
  if (!Number.isFinite(major) || major < 20) {
    fail(`需要 Node.js >= 20，当前为 ${process.versions.node}`, EXIT.USAGE);
  }
}

export function maskSecret(value) {
  const s = String(value ?? '');
  if (!s) return '';
  if (s.length <= 8) return '***';
  return `${s.slice(0, 3)}***${s.slice(-2)}`;
}

export function loadCredentials(env = process.env, { required = true } = {}) {
  const baseUrl = String(env.JIRA_BASE_URL ?? '').trim().replace(/\/+$/, '');
  const token = String(env.JIRA_PAT ?? env.JIRA_TOKEN ?? '').trim();
  const missing = [];
  if (!baseUrl) missing.push('JIRA_BASE_URL');
  if (!token) missing.push('JIRA_PAT');
  if (missing.length && required) {
    fail(
      `缺少环境变量：${missing.join('、')}。\n` +
        '请先设置后重试：\n' +
        '  export JIRA_BASE_URL="https://jira.example.com"\n' +
        '  export JIRA_PAT="<你的 Personal Access Token>"\n' +
        '凭据只从环境变量读取，不接受命令行参数。',
      EXIT.USAGE,
    );
  }
  if (baseUrl && !/^https?:\/\//i.test(baseUrl)) {
    fail(`JIRA_BASE_URL 必须是 http(s) 开头的完整地址（当前 ${maskSecret(baseUrl)}）。`, EXIT.USAGE);
  }
  return { baseUrl, token, missing };
}

export function parseIssueKey(input) {
  const raw = String(input ?? '').trim();
  if (!raw) fail('缺少 Jira Issue Key 或 URL。', EXIT.USAGE);
  const fromUrl = raw.match(/\/browse\/([A-Z][A-Z0-9_]+-\d+)/i);
  const key = (fromUrl ? fromUrl[1] : raw).trim().toUpperCase();
  if (!/^[A-Z][A-Z0-9_]+-\d+$/.test(key)) {
    fail(`无法解析有效的 Jira Issue Key：${raw}`, EXIT.USAGE);
  }
  return key;
}

export function parseArgs(argv) {
  const args = { _: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const item = argv[i];
    if (item === '--') {
      args._.push(...argv.slice(i + 1));
      break;
    }
    if (item.startsWith('--')) {
      const name = item.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith('--')) {
        args[name] = true;
      } else {
        if (args[name] === undefined) args[name] = next;
        else if (Array.isArray(args[name])) args[name].push(next);
        else args[name] = [args[name], next];
        i += 1;
      }
    } else {
      args._.push(item);
    }
  }
  return args;
}

export function boolArg(args, name) {
  return args[name] === true || args[name] === 'true' || args[name] === '1';
}

export function isInside(child, parent) {
  const rel = path.relative(path.resolve(parent), path.resolve(child));
  return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel));
}

export function skillDir() {
  if (process.env.CLAUDE_SKILL_DIR) return path.resolve(process.env.CLAUDE_SKILL_DIR);
  // bin/lib/env.mjs → 技能根目录
  return path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
}

function normalizeWindowsPath(value) {
  const raw = String(value ?? '');
  if (process.platform !== 'win32') return raw;
  const m = raw.match(/^\/([a-zA-Z])\/(.*)$/);
  return m ? `${m[1].toUpperCase()}:/${m[2]}` : raw;
}

export function resolveWorkspace(explicit) {
  const chosen = normalizeWindowsPath(explicit || process.env.QA_WORKSPACE || process.cwd());
  const resolved = path.resolve(chosen);
  if (isInside(resolved, skillDir())) {
    fail('工作区不能位于技能安装目录内；请设置 QA_WORKSPACE 或换一个执行目录。', EXIT.USAGE);
  }
  return resolved;
}

export function sanitizeSegment(value, fallback = '') {
  const s = String(value ?? '')
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/[. ]+$/g, '');
  const reserved = /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$/i.test(s);
  const safe = reserved || !s ? fallback : s;
  return safe.slice(0, 80);
}

export function normalizeScope(value) {
  if (!value) return '';
  const raw = String(value).replace(/\\/g, '/').trim().replace(/^\/+|\/+$/g, '');
  if (!raw) return '';
  if (path.isAbsolute(raw) || /^[A-Za-z]:/.test(raw)) fail(`scope 不能是绝对路径：${value}`, EXIT.USAGE);
  const parts = raw.split('/');
  if (parts.some((p) => !p || p === '.' || p === '..')) {
    fail(`scope 含有非法路径片段：${value}`, EXIT.USAGE);
  }
  return parts.map((p) => sanitizeSegment(p, 'scope')).join('/');
}

export function parseVersion(value) {
  const s = String(value ?? '').trim();
  const m = s.match(/^v?(\d+)\.(\d+)$/);
  if (!m) fail(`版本号格式应为 x.y（如 1.0）：${value}`, EXIT.USAGE);
  return { major: Number(m[1]), minor: Number(m[2]), text: `${Number(m[1])}.${Number(m[2])}` };
}

export function nextVersion(current) {
  const v = parseVersion(current || '1.0');
  return `${v.major}.${v.minor + 1}`;
}

export function timestamp({ offset = process.env.QA_TZ_OFFSET || '+08:00', date = new Date() } = {}) {
  const sign = offset.startsWith('-') ? -1 : 1;
  const m = offset.match(/^([+-])(\d{2}):?(\d{2})$/);
  const hours = m ? Number(m[2]) : 8;
  const minutes = m ? Number(m[3]) : 0;
  const shifted = new Date(date.getTime() + sign * (hours * 60 + minutes) * 60 * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return (
    `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())} ` +
    `${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())} ${offset}`
  );
}

export function dateStamp(date = new Date()) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
}

export async function ensureDir(dir) {
  await mkdir(dir, { recursive: true });
  return dir;
}

export function requireFile(file, label = '文件') {
  if (!file || !existsSync(file)) fail(`找不到${label}：${file}`, EXIT.USAGE);
  return file;
}

export function log(scope, message) {
  process.stderr.write(`[${scope}] ${message}\n`);
}
