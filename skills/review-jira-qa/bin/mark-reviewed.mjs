#!/usr/bin/env node
/**
 * 人工复核标记：记录方案与 XMind 的哈希，阶段三据此确认「复核后未被改动」。
 */

import path from 'node:path';
import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';

import { EXIT, QaError, boolArg, checkNode, parseArgs, parseIssueKey, resolveWorkspace } from './lib/env.mjs';
import { readJson, readText, sha256, writeJson } from './lib/fs-safe.mjs';
import { timestamp } from './lib/env.mjs';
import { handleFatal } from './lib/fatal.mjs';

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
}

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: "design", comment: false, script: "mark-reviewed" }),
);
