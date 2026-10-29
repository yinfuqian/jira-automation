#!/usr/bin/env node
/**
 * 人工复核标记：记录方案与 XMind 的哈希，阶段三据此确认「复核后未被改动」。
 * --jira 时同时写入 Jira 复核评论 QA-测试方案复核通过v<版本>，供线上阶段三使用。
 */

import path from 'node:path';
import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';

import { EXIT, QaError, boolArg, checkNode, loadCredentials, parseArgs, parseIssueKey, resolveWorkspace } from './lib/env.mjs';
import { readJson, readText, sha256, writeJson } from './lib/fs-safe.mjs';
import { timestamp } from './lib/env.mjs';
import { JiraClient } from './lib/jira.mjs';
import { syncStatusComment } from './lib/comment-sync.mjs';
import { handleFatal } from './lib/fatal.mjs';

function planVersion(file) {
  const match = path.basename(file).match(/-测试方案-v(\d+\.\d+)\.md$/i);
  return match ? match[1] : '';
}

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const workspace = resolveWorkspace(args.workspace);
  const key = parseIssueKey(args.key || args._[0]);
  const markerPath = path.join(workspace, '.qa', 'reviews', `${key}-design.json`);

  if (boolArg(args, 'check')) {
    if (!existsSync(markerPath)) throw new QaError(`缺少人工复核标记：${markerPath}`, EXIT.PRECONDITION);
    const marker = await readJson(markerPath);
    const planHash = sha256(await readText(marker.plan));
    const xmindHash = sha256(await readFile(marker.xmind));
    if (planHash !== marker.planSha256 || xmindHash !== marker.xmindSha256) {
      throw new QaError('方案或 XMind 已变化，复核标记失效。', EXIT.PRECONDITION);
    }
    process.stdout.write(`${JSON.stringify({ ok: true, marker })}\n`);
    return;
  }

  const plan = args.plan ? path.resolve(workspace, args.plan) : '';
  const xmind = args.xmind ? path.resolve(workspace, args.xmind) : '';
  if (!plan || !xmind) throw new QaError('需要 --plan <方案> 与 --xmind <XMind>。', EXIT.USAGE);
  const marker = {
    key,
    plan,
    planSha256: sha256(await readText(plan)),
    xmind,
    xmindSha256: sha256(await readFile(xmind)),
    reviewer: args.reviewer || process.env.USER || '',
    reviewedAt: timestamp(),
    note: args.note || '',
  };
  await writeJson(markerPath, marker);
  process.stdout.write(`REVIEW_MARKER=${markerPath}\n`);

  if (boolArg(args, 'jira')) {
    const version = planVersion(plan);
    if (!version) throw new QaError('方案文件名缺少 -测试方案-v<版本>.md，无法写 Jira 复核评论。', EXIT.USAGE);
    const client = new JiraClient(loadCredentials());
    const body = [
      `h3. QA-测试方案复核通过v${version}`,
      '*复核结论：通过*',
      `复核人：${marker.reviewer || '未知'}`,
      `方案：{{${path.basename(plan)}}}`,
      `XMind：{{${path.basename(xmind)}}}`,
      `复核时间：${marker.reviewedAt}`,
    ].join('\n');
    const out = await syncStatusComment({
      client,
      key,
      body,
      runId: args['run-id'] || 'manual',
      workspace,
    });
    process.stdout.write(`JIRA_COMMENT_ID=${out.commentId}\n`);
  }
}

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: 'design', script: 'mark-reviewed' }),
);
