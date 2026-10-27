/**
 * 最小 Jira Server/DC 客户端（零依赖）。
 *
 * - GET 失败按 429/5xx 重试；写操作不自动重试。
 * - 下载前校验 content URL 与 JIRA_BASE_URL 同源。
 * - 所有错误映射到统一退出码。
 */

import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';

import { EXIT, QaError, fail, log, maskSecret } from './env.mjs';

const FIELDS = ['summary', 'description', 'status', 'components', 'labels', 'attachment', 'comment', 'issuetype', 'updated'];

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function mimeOf(file) {
  const ext = path.extname(file).toLowerCase();
  return (
    {
      '.html': 'text/html',
      '.htm': 'text/html',
      '.md': 'text/markdown',
      '.txt': 'text/plain',
      '.json': 'application/json',
      '.csv': 'text/csv',
      '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      '.xmind': 'application/octet-stream',
      '.zip': 'application/zip',
      '.png': 'image/png',
      '.jpg': 'image/jpeg',
      '.jpeg': 'image/jpeg',
      '.pdf': 'application/pdf',
    }[ext] || 'application/octet-stream'
  );
}

export class JiraClient {
  constructor({ baseUrl, token, timeoutMs = Number(process.env.QA_HTTP_TIMEOUT_MS || 30000) }) {
    this.baseUrl = String(baseUrl).replace(/\/+$/, '');
    this.token = token;
    this.timeoutMs = Number.isFinite(timeoutMs) ? timeoutMs : 30000;
    this.origin = new URL(this.baseUrl).origin;
  }

  headers(extra = {}) {
    return { Authorization: `Bearer ${this.token}`, Accept: 'application/json', ...extra };
  }

  async request(apiPath, { method = 'GET', headers = {}, body, retry = method === 'GET' } = {}) {
    const url = `${this.baseUrl}${apiPath.startsWith('/') ? '' : '/'}${apiPath}`;
    const attempts = retry ? 3 : 1;
    let lastError;
    for (let attempt = 1; attempt <= attempts; attempt += 1) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), this.timeoutMs);
      try {
        const res = await fetch(url, { method, headers: this.headers(headers), body, signal: controller.signal });
        const text = await res.text();
        if (res.ok) {
          if (!text) return null;
          try {
            return JSON.parse(text);
          } catch {
            return text;
          }
        }
        if (res.status === 401 || res.status === 403) {
          fail(`Jira 鉴权失败（HTTP ${res.status}）。请检查 JIRA_PAT（${maskSecret(this.token)}）。`, EXIT.AUTH);
        }
        if (res.status === 404) fail(`Jira 资源不存在（HTTP 404）：${apiPath}`, EXIT.NOT_FOUND);
        if (res.status === 429 || res.status >= 500) {
          const retryAfter = Number(res.headers.get('retry-after') || 0);
          lastError = new QaError(`Jira 暂时不可用（HTTP ${res.status}）`, EXIT.NETWORK);
          if (attempt < attempts) {
            await sleep(Math.max(retryAfter * 1000, 800 * attempt));
            continue;
          }
        }
        fail(`Jira 请求失败（HTTP ${res.status}）：${text.slice(0, 300)}`, EXIT.NETWORK);
      } catch (error) {
        if (error instanceof QaError) throw error;
        lastError = new QaError(`Jira 请求异常：${error.message}`, EXIT.NETWORK);
        if (attempt < attempts) {
          await sleep(800 * attempt);
          continue;
        }
      } finally {
        clearTimeout(timer);
      }
    }
    throw lastError || new QaError('Jira 请求失败', EXIT.NETWORK);
  }

  async getIssue(key, { comments = true } = {}) {
    const fields = comments ? FIELDS : FIELDS.filter((f) => f !== 'comment');
    const data = await this.request(`/rest/api/2/issue/${encodeURIComponent(key)}?fields=${fields.join(',')}`);
    return data;
  }

  async getComments(key) {
    const data = await this.request(`/rest/api/2/issue/${encodeURIComponent(key)}/comment`);
    return data?.comments || [];
  }

  async getAttachment(id) {
    return this.request(`/rest/api/2/attachment/${encodeURIComponent(id)}`);
  }

  async downloadAttachment(id, dest, { maxMb = Number(process.env.QA_MAX_ATTACHMENT_MB || 50) } = {}) {
    const meta = await this.getAttachment(id);
    if (!meta?.content) fail(`附件 ${id} 没有 content 地址。`, EXIT.NETWORK);
    let contentUrl;
    try {
      contentUrl = new URL(meta.content);
    } catch {
      fail(`附件 ${id} 的 content 地址非法。`, EXIT.NETWORK);
    }
    if (contentUrl.origin !== this.origin) {
      fail(`附件 ${id} 的下载地址与 JIRA_BASE_URL 不同源，已拒绝。`, EXIT.AUTH);
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const res = await fetch(contentUrl, { headers: { Authorization: `Bearer ${this.token}` }, signal: controller.signal });
      if (!res.ok) fail(`附件 ${id} 下载失败（HTTP ${res.status}）。`, EXIT.NETWORK);
      const buf = Buffer.from(await res.arrayBuffer());
      if (buf.length > maxMb * 1024 * 1024) {
        fail(`附件 ${id} 超过 ${maxMb}MB 上限。`, EXIT.VALIDATION);
      }
      await writeFile(dest, buf);
      return { meta, bytes: buf.length, path: dest };
    } finally {
      clearTimeout(timer);
    }
  }

  async uploadAttachment(key, file, { mime } = {}) {
    const buf = await readFile(file);
    const fd = new FormData();
    fd.append('file', new Blob([buf], { type: mime || mimeOf(file) }), path.basename(file));
    const res = await this.request(`/rest/api/2/issue/${encodeURIComponent(key)}/attachments`, {
      method: 'POST',
      headers: { 'X-Atlassian-Token': 'no-check' },
      body: fd,
      retry: false,
    });
    const first = Array.isArray(res) ? res[0] : null;
    if (!first?.id) fail(`附件上传失败：${path.basename(file)}`, EXIT.REMOTE_WRITE);
    return first;
  }

  async deleteAttachment(id) {
    return this.request(`/rest/api/2/attachment/${encodeURIComponent(id)}`, { method: 'DELETE', retry: false });
  }

  async createComment(key, body) {
    return this.request(`/rest/api/2/issue/${encodeURIComponent(key)}/comment`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ body }),
      retry: false,
    });
  }

  async updateComment(key, id, body) {
    return this.request(`/rest/api/2/issue/${encodeURIComponent(key)}/comment/${encodeURIComponent(id)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ body }),
      retry: false,
    });
  }
}

export function attachmentList(issue) {
  return (issue?.fields?.attachment || []).map((a) => ({
    id: String(a.id),
    filename: a.filename,
    size: Number(a.size || 0),
    created: a.created,
    author: a.author?.displayName || '',
    mimeType: a.mimeType || '',
  }));
}

export async function withClient(fn) {
  const { loadCredentials } = await import('./env.mjs');
  const creds = loadCredentials();
  const client = new JiraClient(creds);
  log('jira', `base=${creds.baseUrl} token=${maskSecret(creds.token)}`);
  return fn(client);
}
