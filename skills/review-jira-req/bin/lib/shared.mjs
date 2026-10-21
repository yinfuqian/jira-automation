/**
 * 共享工具：凭据读取、PAT 掩码、运行 id、日志输出。
 *
 * 安全约束（见 specs/jira-requirement-review/spec.md → 凭据与数据安全）：
 * - 凭据只从环境变量读取，不接受命令行参数传入。
 * - 任何输出都必须经过 maskSecret()，保证 PAT 不以明文形式出现在终端或文件里。
 */

import { randomBytes } from 'node:crypto';

export const REQUIRED_ENV_VARS = ['JIRA_BASE_URL', 'JIRA_PAT'];

/**
 * 服务端锁定标记：为 "1" 时凭据只认后端注入的 JIRA_TOKEN 与 JIRA_BASE_URL，
 * 不再接受 JIRA_PAT 这个第二来源（见 backend/app/skillrun/env_lock.py）。
 */
export function envLocked(env = process.env) {
  return String(env.SKILLRUN_ENV_LOCKED || '').trim() === '1';
}

/**
 * 从环境变量读取 JIRA 凭据。
 * 缺失时抛出错误，错误信息包含缺失的变量名（但绝不包含任何变量值）。
 */
export function loadCredentials(env = process.env) {
  // 锁定模式（容器内由后端注入）下令牌统一取 JIRA_TOKEN，避免出现第二个凭据来源。
  const locked = envLocked(env);
  const patVar = locked ? 'JIRA_TOKEN' : 'JIRA_PAT';
  const requiredVars = locked ? ['JIRA_BASE_URL', 'JIRA_TOKEN'] : REQUIRED_ENV_VARS;
  const missing = requiredVars.filter((name) => {
    const value = env[name];
    return value === undefined || value === null || String(value).trim() === '';
  });

  if (missing.length > 0) {
    const patHint = locked
      ? '  （运行环境已锁定：令牌只认服务端注入的 JIRA_TOKEN，不接受令牌文件与命令行参数）\n'
      : '  export JIRA_PAT="<你的 Personal Access Token>"\n';
    throw new CredentialsError(
      `缺少必需的环境变量：${missing.join('、')}。\n` +
        '请先设置后重试，例如：\n' +
        '  export JIRA_BASE_URL="https://jira.example.com"\n' +
        patHint +
        '出于安全考虑，PAT 只能通过环境变量提供，不接受命令行参数。',
      missing,
    );
  }

  const baseUrl = String(env.JIRA_BASE_URL).trim().replace(/\/+$/, '');
  const pat = String(env[patVar]);

  if (!/^https?:\/\//i.test(baseUrl)) {
    throw new CredentialsError(
      `JIRA_BASE_URL 必须是 http(s) 开头的完整地址，当前值形如 "${redactUrl(baseUrl)}"。`,
      ['JIRA_BASE_URL'],
    );
  }

  return { baseUrl, pat };
}

export class CredentialsError extends Error {
  constructor(message, missingVars = []) {
    super(message);
    this.name = 'CredentialsError';
    this.missingVars = missingVars;
  }
}

/**
 * 掩码任意敏感字符串，只保留首尾少量字符。
 * 用于日志与错误信息，确保 PAT 不以明文出现。
 */
export function maskSecret(value) {
  if (value === undefined || value === null) return '';
  const text = String(value);
  if (text.length === 0) return '';
  if (text.length <= 8) return '*'.repeat(text.length);
  return `${text.slice(0, 4)}${'*'.repeat(Math.min(text.length - 8, 32))}${text.slice(-4)}`;
}

const URL_CREDENTIALS = /(\/\/)[^/@\s]+(:[^/@\s]*)?@/g;

/** 去掉 URL 里可能内嵌的凭据片段。 */
export function redactUrl(url) {
  return String(url).replace(URL_CREDENTIALS, '$1');
}

/**
 * 对任意准备输出的文本做最后一道防线：
 * 替换已知敏感值，并抹掉 URL 内嵌凭据。
 */
export function redact(text, secrets = []) {
  let output = redactUrl(String(text));
  for (const secret of secrets) {
    if (!secret) continue;
    const value = String(secret);
    if (value.length === 0) continue;
    output = output.split(value).join(maskSecret(value));
  }
  return output;
}

/** 生成运行 id：可排序的时间戳 + 随机后缀，避免并发运行互相覆盖。 */
export function makeRunId(now = new Date()) {
  const pad = (n, width = 2) => String(n).padStart(width, '0');
  const stamp =
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}` +
    `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  return `${stamp}-${randomBytes(3).toString('hex')}`;
}

/** 生成报告文件名用的时间戳，精确到秒。 */
export function makeTimestamp(now = new Date()) {
  const pad = (n, width = 2) => String(n).padStart(width, '0');
  return (
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}` +
    `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
  );
}

/**
 * 统一的输出入口。所有面向用户的文本都应经过它，
 * 以便在此集中做敏感信息掩码。
 */
export function createLogger({ secrets = [], stream = process.stderr } = {}) {
  const write = (text) => stream.write(`${redact(text, secrets)}\n`);
  return {
    info: (message) => write(message),
    warn: (message) => write(`[警告] ${message}`),
    error: (message) => write(`[错误] ${message}`),
    /** 面向报告/终端摘要的正式输出，默认写 stdout。 */
    out: (message) => process.stdout.write(`${redact(message, secrets)}\n`),
  };
}

// 允许直接以脚本方式运行时自检：`node scripts/lib/shared.mjs --selftest`
if (process.argv[1] && process.argv[1].endsWith('shared.mjs') && process.argv.includes('--selftest')) {
  const assert = (label, actual, expected) => {
    const ok = actual === expected;
    console.log(`${ok ? 'PASS' : 'FAIL'} ${label}: ${actual}`);
    if (!ok) {
      console.log(`     期望: ${expected}`);
      process.exitCode = 1;
    }
  };
  assert('maskSecret(16 字符)', maskSecret('abcdefghijklmnop'), 'abcd********mnop');
  assert('maskSecret(4 字符)', maskSecret('abcd'), '****');
  assert('maskSecret(空)', maskSecret(''), '');
  assert('redactUrl 剥离内嵌凭据', redactUrl('https://user:pw@jira.example.com/x'), 'https://jira.example.com/x');
  assert('redact 替换 PAT 明文', redact('token=SEKRET123', ['SEKRET123']), 'token=SEKR*T123');
  assert('makeRunId 形如 YYYYMMDD-HHMMSS-xxxxxx', /^20260920-100221-[0-9a-f]{6}$/.test(makeRunId(new Date(2026, 8, 20, 10, 2, 21))), true);
  assert('makeRunId 并发不重复', makeRunId() === makeRunId(), false);
  assert('makeTimestamp 形如 YYYYMMDD-HHMMSS', makeTimestamp(new Date(2026, 8, 20, 10, 2, 21)), '20260920-100221');
}
