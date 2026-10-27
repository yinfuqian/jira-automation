#!/usr/bin/env node
/**
 * 汇总一次运行的结果，写 .qa/runs/<runId>/result.json 并打印 QA_RESULT_JSON。
 * 结论=暂不通过 仍是 ok=true（业务完成）；运营性失败才 ok=false。
 */

import path from 'node:path';

import { EXIT, QaError, checkNode, parseArgs, resolveWorkspace } from './lib/env.mjs';
import { readJson } from './lib/fs-safe.mjs';
import { printResult, writeResult } from './lib/result.mjs';
import { handleFatal } from './lib/fatal.mjs';

async function maybeJson(file) {
  try {
    return await readJson(file);
  } catch {
    return null;
  }
}

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const workspace = resolveWorkspace(args.workspace);
  const runId = args['run-id'] || 'manual';
  const runDir = path.join(workspace, '.qa', 'runs', runId);
  const materials = await maybeJson(path.join(runDir, 'materials.json'));
  const upload = await maybeJson(path.join(runDir, 'upload.json'));
  const comment = await maybeJson(path.join(runDir, 'comment.json'));
  const spec = args.spec ? await maybeJson(args.spec) : null;
  const specMaterialIds = (spec?.materials || []).map((m) => String(m.id)).filter(Boolean);

  const hasError = Boolean(args.error);
  const result = {
    schemaVersion: 1,
    stage: args.stage || 'risk-analysis',
    ok: !hasError,
    jiraKey: args.key || materials?.key || spec?.jiraKey || '',
    requirementName: args.name || materials?.requirementName || spec?.requirementName || '',
    conclusion: args.conclusion || spec?.conclusion || '',
    failedItemCount: Number(args.failed ?? (spec?.failures?.length || 0)),
    deliverableVersion: String(args.version || spec?.deliverableVersion || '1.0').replace(/^v/, ''),
    htmlPath: args.html || upload?.files?.[0]?.path || '',
    attachmentId: args.attachment || upload?.files?.[0]?.id || '',
    commentId: args.comment || comment?.commentId || '',
    materialAttachmentIds: specMaterialIds.length
      ? specMaterialIds
      : (materials?.materials || [])
          .filter((m) => !['excluded', 'unsupported'].includes(m.role))
          .map((m) => m.id),
    nextCommand: args['next-command'] || (args.key ? `/qa-design ${args.key}` : ''),
    warnings: [],
  };
  if (hasError) result.error = String(args.error);
  await writeResult(workspace, runId, result);
  printResult(result);
  if (hasError) return;
}

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: "", script: "result" }),
);
