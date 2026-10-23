#!/usr/bin/env node
/**
 * 同版本材料复用：当前 Jira 附件 ID 集合与上一条门禁评论一致时，
 * 直接复用上一版结论，不重新分析、不新增附件/评论，保证同材料结果稳定。
 */

import path from 'node:path';

import { EXIT, QaError, boolArg, checkNode, loadCredentials, log, parseArgs, parseIssueKey, resolveWorkspace } from './lib/env.mjs';
import { JiraClient } from './lib/jira.mjs';
import { readJson, writeJson } from './lib/fs-safe.mjs';
import { makeRunId, printResult } from './lib/result.mjs';
import { handleFatal } from './lib/fatal.mjs';

function expandIds(text) {
  const ids = new Set();
  const source = String(text || '');
  for (const m of source.matchAll(/(\d{3,})\s*[–~-]\s*(\d{3,})/g)) {
    const a = Number(m[1]);
    const b = Number(m[2]);
    const lo = Math.min(a, b);
    const hi = Math.max(a, b);
    if (hi - lo <= 200) for (let i = lo; i <= hi; i += 1) ids.add(String(i));
  }
  for (const m of source.matchAll(/(?<![–~-])\b(\d{3,})\b(?![–~-])/g)) ids.add(String(m[1]));
  return [...ids];
}

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const key = parseIssueKey(args.key || args._[0]);
  const workspace = resolveWorkspace(args.workspace);
  const materialsPath = args.materials;
  if (!materialsPath) throw new QaError('缺少 --materials <materials.json>', EXIT.USAGE);
  const materials = await readJson(materialsPath);
  const runId = args['run-id'] || materials.runId || makeRunId(key);
  const force = boolArg(args, 'force');

  const current = new Set(
    (materials.materials || [])
      .filter((m) => m.id !== 'issue-description' && !['excluded', 'unsupported'].includes(m.role))
      .map((m) => String(m.id)),
  );
  if (materials.primaryId === 'issue-description') current.add('issue-description');

  const client = new JiraClient(loadCredentials());
  const comments = await client.getComments(key);
  const gateComments = comments.filter((c) => String(c.body || '').trimStart().startsWith('h3. QA-需求准入门禁结果v'));
  const previous = gateComments.length ? gateComments[gateComments.length - 1] : null;

  let reuse = false;
  let previousVersion = '';
  let conclusion = '';
  let previousIds = [];
  let reason = '没有上一版门禁结果';
  if (previous) {
    const body = String(previous.body || '');
    previousVersion = (body.match(/QA-需求准入门禁结果v(\d+\.\d+)/) || [])[1] || '';
    conclusion = (body.match(/结论：\s*([^*\n]+)/) || [])[1]?.trim() || '';
    const materialLine = body.split(/\r?\n/).filter((l) => l.includes('材料版本：')).pop() || '';
    previousIds = expandIds(materialLine);
    const previousDescriptionOnly = materialLine.includes('需求描述取自 Jira 字段') && previousIds.length === 0;
    if (previousDescriptionOnly) previousIds = ['issue-description'];
    const same =
      previousIds.length === current.size &&
      previousIds.every((id) => current.has(id)) &&
      current.size > 0;
    reuse = same;
    reason = same ? '材料附件 ID 集合一致，复用上一版结论' : '材料附件 ID 集合不一致，需要重新分析';
  }

  const result = {
    schemaVersion: 1,
    ok: true,
    stage: 'risk-analysis',
    applicable: true,
    status: 'OK',
    reused: reuse && !force,
    forced: force,
    jiraKey: key,
    conclusion: reuse && !force ? conclusion : '',
    failedItemCount: 0,
    deliverableVersion: previousVersion,
    htmlPath: '',
    attachmentId: '',
    commentId: previous ? String(previous.id) : '',
    materialAttachmentIds: [...current],
    previousMaterialAttachmentIds: previousIds,
    reason: force ? '显式 --force，重新分析' : reason,
    nextCommand: '',
    warnings: [],
  };

  await writeJson(path.join(workspace, '.qa', 'runs', runId, 'reuse.json'), result);
  log('reuse', `reuse=${result.reused} previous=${previousVersion || 'none'} reason=${result.reason}`);
  process.stdout.write(`REUSE=${result.reused}\nPREVIOUS_VERSION=${previousVersion}\nPREVIOUS_COMMENT_ID=${result.commentId}\nCONCLUSION=${result.conclusion}\n`);
  if (result.reused) {
    printResult(result);
  }
}

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: 'risk-analysis', comment: false, script: 'reuse' }),
);
