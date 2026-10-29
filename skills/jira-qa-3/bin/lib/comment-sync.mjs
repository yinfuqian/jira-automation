/**
 * Jira 评论同步：按“标题 + 材料版本行”定位，存在则编辑，否则新建。
 * 阶段一与阶段二共用；阶段二标题与阶段一不同，不会互相覆盖。
 */

import path from 'node:path';

import { ensureDir, writeJson } from './fs-safe.mjs';

export function firstLine(text) {
  return String(text || '').split(/\r?\n/).find((line) => line.trim()) || '';
}

export function lastLine(text) {
  const lines = String(text || '')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  return lines[lines.length - 1] || '';
}

export function parseCommentBody(body) {
  const title = firstLine(body);
  const matchTitle = String(title).replace(/v\d+\.\d+\s*$/, '').trim();
  const last = lastLine(body);
  const materialLine = last.startsWith('材料版本：') ? last : '';
  const materialVersion = (String(materialLine).match(/材料版本：\s*(PRD\s+[^\s（(]+)/) || [])[1] || '';
  return { title, matchTitle, materialLine, materialVersion };
}

export function findExistingComment(comments, body) {
  const { matchTitle, materialLine } = parseCommentBody(body);
  const wanted = materialIdsOf(materialLine);
  return (comments || []).find((comment) => {
    const text = String(comment.body || '');
    if (!text.includes(matchTitle)) return false;
    if (!materialLine) return true;
    // 材料版本按附件 ID 集合比较：同一 PRD 版本号下附件被替换也算新材料，必须新发评论
    const last = lastLine(text);
    const existing = materialIdsOf(last.startsWith('材料版本：') ? last : '');
    if (!existing.length) return false;
    return sameIds(existing, wanted);
  });
}

function materialIdsOf(line) {
  const text = String(line || '');
  if (!text) return [];
  return [...new Set([...text.matchAll(/\d{3,}/g)].map((hit) => hit[0]))].sort();
}

function sameIds(a, b) {
  if (!a.length || a.length !== b.length) return false;
  return a.every((id, index) => id === b[index]);
}

export async function syncComment({
  client,
  key,
  body,
  mode = 'auto',
  editId = '',
  runId = 'manual',
  workspace,
}) {
  const { title, materialLine } = parseCommentBody(body);
  const comments = await client.getComments(key);
  const existing = findExistingComment(comments, body);

  let action = 'create';
  let commentId = null;
  if (existing && mode !== 'new') {
    action = 'edit';
    commentId = String(existing.id);
  } else if (!existing && mode === 'edit') {
    throw new Error(`没有找到匹配的既有评论，无法 edit：${title}`);
  } else if (editId) {
    action = 'edit';
    commentId = String(editId);
  }

  const result = action === 'edit'
    ? await client.updateComment(key, commentId, body)
    : await client.createComment(key, body);
  const id = String(result?.id || commentId || '');
  const out = { ok: true, key, runId, action, commentId: id, title, materialLine };
  if (workspace) {
    const runDir = path.join(workspace, '.qa', 'runs', runId);
    await ensureDir(runDir);
    await writeJson(path.join(runDir, 'comment.json'), out);
  }
  return out;
}

export async function syncStatusComment({ client, key, body, runId = 'manual', workspace }) {
  return syncComment({ client, key, body, runId, workspace });
}
