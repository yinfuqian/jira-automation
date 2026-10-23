#!/usr/bin/env node
/**
 * 同步阶段一 Jira 评论：按标题前缀 + 材料版本文本定位；存在则编辑，否则新建。
 * 同一材料版本只保留一条评论。
 */

import path from 'node:path';

import { EXIT, QaError, boolArg, checkNode, loadCredentials, log, parseArgs, parseIssueKey, resolveWorkspace } from './lib/env.mjs';
import { JiraClient } from './lib/jira.mjs';
import { ensureDir, readText, writeJson } from './lib/fs-safe.mjs';

function firstLine(text) {
  return String(text || '').split(/\r?\n/).find((line) => line.trim()) || '';
}

function lastLine(text) {
  const lines = String(text || '').split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  return lines[lines.length - 1] || '';
}

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const key = parseIssueKey(args.key || args._[0]);
  const workspace = resolveWorkspace(args.workspace);
  const bodyFile = args.body;
  if (!bodyFile) {
    process.stderr.write('[jira-comment][ERR] 缺少 --body <wiki 文件>。\n');
    process.exit(EXIT.USAGE);
  }
  const body = await readText(bodyFile);
  const title = args.title || firstLine(body);
  const materialLine = args['material-line'] || lastLine(body);
  const mode = args.mode || 'auto';
  const dryRun = boolArg(args, 'dry-run') || boolArg(args, 'no-comment');
  const runId = args['run-id'] || 'manual';

  const client = new JiraClient(loadCredentials(process.env, { required: !dryRun }));
  const comments = process.env.JIRA_BASE_URL ? await client.getComments(key) : [];
  const existing = comments.find((c) => {
    const text = String(c.body || '');
    return text.includes(title) && text.includes(materialLine);
  });

  let action = 'create';
  let commentId = null;
  if (existing && mode !== 'new') {
    action = 'edit';
    commentId = String(existing.id);
  } else if (!existing && mode === 'edit') {
    process.stderr.write('[jira-comment][ERR] 没有找到匹配的既有评论，无法 edit。\n');
    process.exit(EXIT.PRECONDITION);
  } else if (args['edit-id']) {
    action = 'edit';
    commentId = String(args['edit-id']);
  }

  if (dryRun) {
    log('jira-comment', `dry-run ${action} title=${title}`);
    process.stdout.write(`ACTION=${action}\nDRY_RUN=1\n`);
    return;
  }

  let result;
  if (action === 'edit') result = await client.updateComment(key, commentId, body);
  else result = await client.createComment(key, body);
  const id = String(result?.id || commentId || '');
  const out = { ok: true, key, runId, action, commentId: id, title, materialLine };
  await writeJson(path.join(workspace, '.qa', 'runs', runId, 'comment.json'), out);
  log('jira-comment', `${action} commentId=${id}`);
  process.stdout.write(`ACTION=${action}\nCOMMENT_ID=${id}\n`);
}

main().catch((error) => {
  const code = error instanceof QaError ? error.code : EXIT.INTERNAL;
  process.stderr.write(`[jira-comment][ERR] ${error.message}\n`);
  process.exit(code === EXIT.OK ? EXIT.REMOTE_WRITE : code);
});
