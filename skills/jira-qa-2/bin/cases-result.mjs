#!/usr/bin/env node
/**
 * 汇总阶段三结果：写 .qa/runs/<runId>/result.json 并打印 QA_RESULT_JSON。
 *
 * 阶段三只产出本地交付包（Excel + Xray CSV + zip），不上传 Jira、不写评论；
 * 因此结果只记录文件路径与用例数，供平台任务详情和调用统计使用。
 */

import { existsSync } from 'node:fs';
import path from 'node:path';

import { checkNode, parseArgs, resolveWorkspace } from './lib/env.mjs';
import { readJson, writeJson } from './lib/fs-safe.mjs';
import { printResult, redact, writeResult } from './lib/result.mjs';
import { handleFatal } from './lib/fatal.mjs';

/** 读取 JSON；文件不存在时返回 null，避免把可选的上下文当成硬依赖。 */
async function maybeJson(file) {
  if (!file || !existsSync(file)) return null;
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
  const runId = String(args['run-id'] || 'manual');
  const runDir = path.join(workspace, '.qa', 'runs', runId);
  const materials = await maybeJson(path.join(runDir, 'materials.json'));
  const cases = await maybeJson(args.cases);
  const hasError = Boolean(args.error);

  const result = {
    schemaVersion: 1,
    stage: 'cases',
    ok: !hasError,
    applicable: true,
    reused: false,
    status: hasError ? 'ERROR' : 'OK',
    jiraKey: String(args.key || cases?.jiraKey || materials?.key || ''),
    requirementName: cases?.requirementName || materials?.requirementName || '',
    conclusion: hasError ? '' : '已生成',
    failedItemCount: 0,
    pendingItemCount: 0,
    deliverableVersion: String(args.version || '').replace(/^v/, ''),
    htmlPath: '',
    planPath: String(args.plan || ''),
    xmindPath: String(args.xmind || ''),
    xlsxPath: String(args.xlsx || ''),
    csvPath: String(args.csv || ''),
    zipPath: String(args.zip || ''),
    casesCount: Number(cases?.cases?.length || 0),
    attachmentId: '',
    commentId: '',
    materialAttachmentIds: [],
    errorCode: hasError ? 'INTERNAL' : '',
    error: hasError ? String(args.error) : '',
    reason: hasError ? String(args.error) : '',
    nextCommand: '',
    warnings: [],
  };

  await writeResult(workspace, runId, result);
  await writeJson(path.join(runDir, 'cases-result.json'), redact(result));
  printResult(result);
}

main().catch((error) => handleFatal(error, { argv: process.argv.slice(2), stage: 'cases', comment: false, script: 'cases-result' }));
