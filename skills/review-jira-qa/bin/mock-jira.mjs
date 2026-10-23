#!/usr/bin/env node
/**
 * 本地 mock Jira Server/DC（仅 loopback，测试用，不是运行时服务）。
 *
 * 覆盖：读单、附件下载、附件上传、附件删除、评论读取/新建/编辑、401/404。
 */

import { createServer } from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { EXIT } from './lib/env.mjs';

export const MOCK_PAT = 'mock-pat-do-not-use-in-production';
const PRD = `# 示例需求 PRD\n\n## 1. 背景\n这是 mock 需求材料。\n\n## 2. 规则\n- 规则一：必须满足条件 A。\n- 规则二：异常时提示 B。\n`;
const OVERVIEW = `<!doctype html><html><body><h1>示例需求 Overview</h1><p>页面说明：列表支持筛选与导出。</p><script>const t = "脚本内正文：提交后提示成功";</script></body></html>`;

function makeState() {
  const attachments = new Map();
  const add = (id, filename, content) => {
    attachments.set(String(id), {
      id: String(id),
      filename,
      content: Buffer.from(content),
      created: '2026-10-20T14:00:00.000+0800',
      mimeType: filename.endsWith('.html') ? 'text/html' : 'text/markdown',
    });
  };
  add('1001', '示例需求_PRD_DELIVERY.md', PRD);
  add('1002', '示例需求_Overview.html', OVERVIEW);
  const comments = [
    { id: '2001', author: { displayName: '产品' }, created: '2026-10-20T15:00:00.000+0800', body: '产品补充说明。' },
  ];
  return { attachments, comments, nextAttachmentId: 1003, nextCommentId: 2002 };
}

function issueJson(state, base) {
  return {
    key: 'QA-101',
    fields: {
      summary: '【示例模块】支持列表筛选与导出',
      status: { name: '评审中' },
      issuetype: { name: '故事' },
      components: [{ name: '示例模块' }],
      labels: ['QA'],
      updated: '2026-10-20T15:00:00.000+0800',
      attachment: [...state.attachments.values()].map((a) => ({
        id: a.id,
        filename: a.filename,
        size: a.content.length,
        created: a.created,
        mimeType: a.mimeType,
        author: { displayName: '产品' },
        content: `${base}/mock/attachment/${a.id}`,
      })),
      comment: state.comments,
    },
  };
}

function parseMultipart(buffer, contentType) {
  const boundaryMatch = /boundary=(?:"([^"]+)"|([^;]+))/i.exec(contentType || '');
  const boundary = boundaryMatch ? (boundaryMatch[1] || boundaryMatch[2]).trim() : '';
  if (!boundary) return null;
  const delim = Buffer.from(`--${boundary}`);
  let start = buffer.indexOf(delim);
  while (start !== -1) {
    const next = buffer.indexOf(delim, start + delim.length);
    if (next === -1) break;
    const part = buffer.subarray(start + delim.length, next);
    const headerEnd = part.indexOf('\r\n\r\n');
    if (headerEnd !== -1) {
      const headers = part.subarray(0, headerEnd).toString('utf8');
      const name = /name="([^"]+)"/i.exec(headers);
      const filename = /filename="([^"]*)"/i.exec(headers);
      if (name && name[1] === 'file') {
        let data = part.subarray(headerEnd + 4);
        if (data.subarray(-2).toString() === '\r\n') data = data.subarray(0, -2);
        return { filename: filename ? filename[1] : 'upload.bin', data };
      }
    }
    start = next;
  }
  return null;
}

export function startMock({ port = 0 } = {}) {
  const state = makeState();
  let base = '';
  const server = createServer((req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    const send = (status, body, headers = {}) => {
      const data = Buffer.isBuffer(body) ? body : Buffer.from(JSON.stringify(body));
      res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', ...headers });
      res.end(data);
    };
    if (url.pathname === '/healthz') return send(200, { ok: true });
    if (req.headers.authorization !== `Bearer ${MOCK_PAT}`) return send(401, { errorMessages: ['unauthorized'] });

    const issueMatch = url.pathname.match(/^\/rest\/api\/2\/issue\/(QA-101)$/);
    if (issueMatch && req.method === 'GET') return send(200, issueJson(state, base));
    if (url.pathname === '/rest/api/2/issue/QA-101/comment' && req.method === 'GET') {
      return send(200, { comments: state.comments });
    }
    if (url.pathname === '/rest/api/2/issue/QA-101/comment' && req.method === 'POST') {
      let body = '';
      req.on('data', (c) => (body += c));
      req.on('end', () => {
        const parsed = JSON.parse(body || '{}');
        const comment = { id: String(state.nextCommentId++), author: { displayName: 'QA' }, created: new Date().toISOString(), body: parsed.body || '' };
        state.comments.push(comment);
        send(201, comment);
      });
      return;
    }
    const commentEdit = url.pathname.match(/^\/rest\/api\/2\/issue\/QA-101\/comment\/(\d+)$/);
    if (commentEdit && req.method === 'PUT') {
      let body = '';
      req.on('data', (c) => (body += c));
      req.on('end', () => {
        const parsed = JSON.parse(body || '{}');
        const target = state.comments.find((c) => c.id === commentEdit[1]);
        if (!target) return send(404, { errorMessages: ['not found'] });
        target.body = parsed.body || '';
        send(200, target);
      });
      return;
    }
    const metaMatch = url.pathname.match(/^\/rest\/api\/2\/attachment\/(\d+)$/);
    if (metaMatch && req.method === 'GET') {
      const att = state.attachments.get(metaMatch[1]);
      if (!att) return send(404, { errorMessages: ['not found'] });
      return send(200, {
        id: att.id,
        filename: att.filename,
        size: att.content.length,
        created: att.created,
        mimeType: att.mimeType,
        content: `${base}/mock/attachment/${att.id}`,
      });
    }
    if (metaMatch && req.method === 'DELETE') {
      if (!state.attachments.delete(metaMatch[1])) return send(404, { errorMessages: ['not found'] });
      res.writeHead(204);
      return res.end();
    }
    const contentMatch = url.pathname.match(/^\/mock\/attachment\/(\d+)$/);
    if (contentMatch && req.method === 'GET') {
      const att = state.attachments.get(contentMatch[1]);
      if (!att) return send(404, { errorMessages: ['not found'] });
      res.writeHead(200, { 'Content-Type': att.mimeType });
      return res.end(att.content);
    }
    if (url.pathname === '/rest/api/2/issue/QA-101/attachments' && req.method === 'POST') {
      const chunks = [];
      req.on('data', (c) => chunks.push(c));
      req.on('end', () => {
        const parsed = parseMultipart(Buffer.concat(chunks), req.headers['content-type']);
        if (!parsed) return send(400, { errorMessages: ['bad multipart'] });
        const id = String(state.nextAttachmentId++);
        state.attachments.set(id, {
          id,
          filename: parsed.filename,
          content: parsed.data,
          created: new Date().toISOString(),
          mimeType: 'application/octet-stream',
        });
        send(200, [{ id, filename: parsed.filename, size: parsed.data.length }]);
      });
      return;
    }
    if (url.pathname === '/mock/state' && req.method === 'GET') {
      return send(200, {
        attachments: [...state.attachments.values()].map((a) => ({ id: a.id, filename: a.filename, size: a.content.length })),
        comments: state.comments,
      });
    }
    send(404, { errorMessages: [`no mock route: ${req.method} ${url.pathname}`] });
  });
  return new Promise((resolve) => {
    server.listen(port, '127.0.0.1', () => {
      base = `http://127.0.0.1:${server.address().port}`;
      resolve({ server, port: server.address().port, state });
    });
  });
}

if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url))) {
  const args = process.argv.slice(2);
  const portArg = args.indexOf('--port');
  const port = portArg >= 0 ? Number(args[portArg + 1]) : 0;
  startMock({ port }).then(({ port: actual }) => {
    const base = `http://127.0.0.1:${actual}`;
    process.stdout.write(`MOCK_JIRA=${base}\n`);
    process.stdout.write(`export JIRA_BASE_URL="${base}"\nexport JIRA_PAT="${MOCK_PAT}"\nexport QA_WORKSPACE="$(pwd)"\n`);
    if (!args.includes('--print-env')) process.stdout.write('mock jira running (Ctrl+C to stop)\n');
  }).catch((error) => {
    process.stderr.write(`[mock-jira][ERR] ${error.message}\n`);
    process.exit(EXIT.INTERNAL);
  });
}
