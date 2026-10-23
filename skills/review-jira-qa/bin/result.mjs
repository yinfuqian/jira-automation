#!/usr/bin/env node
/**
 * 汇总一次运行的结果，写 .qa/runs/<runId>/result.json 并打印 QA_RESULT_JSON。
 * 结论=暂不通过 仍是 ok=true（业务完成）；运营性失败才 ok=false。
 * 加 --non-blocking 时，即使 ok=false 也以 0 退出，供父技能继续执行后续流程。
 */

import path from 'node:path';

import { EXIT, QaError, checkNode, parseArgs, resolveWorkspace } from './lib/env.mjs';
import { readJson } from './lib/fs-safe.mjs';
import { printResult, writeResult } from './lib/result.mjs';

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

  const hasError = Boolean(args.error);
  // 父技能调用时使用非阻塞模式：错误仍写入 ok/error 字段，但不再用退出码中断调用方。
  const nonBlocking = args['non-blocking'] === true || args['non-blocking'] === 'true';
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
    materialAttachmentIds: (materials?.materials || []).filter((m) => m.role !== 'excluded').map((m) => m.id),
    nextCommand: args['next-command'] || (args.key ? `/qa-design ${args.key}` : ''),
    warnings: [],
    failedStep: args['failed-step'] ? String(args['failed-step']) : '',
    nonBlocking,
  };
  if (hasError) result.error = String(args.error);
  await writeResult(workspace, runId, result);
  printResult(result);
  // 非阻塞模式下始终返回 0；调用方必须读取 result.json / QA_RESULT_JSON 的 ok 字段判断结果。
  if (hasError && !nonBlocking) process.exit(Number(args.code || EXIT.INTERNAL));
}

main().catch((error) => {
  const code = error instanceof QaError ? error.code : EXIT.INTERNAL;
  process.stderr.write(`[result][ERR] ${error.message}\n`);
  process.exit(code);
});
