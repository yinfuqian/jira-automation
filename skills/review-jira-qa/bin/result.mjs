#!/usr/bin/env node
/**
 * 汇总一次运行的结果，写 .qa/runs/<runId>/result.json 并打印 QA_RESULT_JSON。
 * 阶段一：结论=暂不通过 仍是 ok=true（业务完成）；运营性失败才 ok=false。
 * 阶段二：generated/skipped 为 ok=true；failed 为 ok=false；不满足条件为 SKIPPED。
 */

import path from 'node:path';

import { EXIT, QaError, checkNode, parseArgs, resolveWorkspace } from './lib/env.mjs';
import { readJson } from './lib/fs-safe.mjs';
import { printResult, writeResult } from './lib/result.mjs';
import { effectiveIds, DESCRIPTION_ID } from './lib/material-set.mjs';
import { handleFatal } from './lib/fatal.mjs';

async function maybeJson(file) {
  try {
    return await readJson(file);
  } catch {
    return null;
  }
}

function designStatus(spec) {
  if (spec.status === 'reused') return { status: 'OK', ok: true, applicable: true, reused: true };
  if (spec.status === 'not-satisfied') return { status: 'SKIPPED', ok: true, applicable: false, reused: false };
  return { status: 'OK', ok: true, applicable: true, reused: false };
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
  const isDesignSpec = Boolean(spec && (spec.status || spec.planFile || spec.commentTitle));
  const stage = args.stage || (isDesignSpec ? 'design' : 'risk-analysis');
  const hasError = Boolean(args.error);

  let result;
  if (isDesignSpec) {
    const mapped = designStatus(spec);
    const ids = Array.isArray(spec.materials) ? spec.materials : materials ? effectiveIds(materials) : [];
    const uploaded = Array.isArray(upload?.files) ? upload.files : [];
    result = {
      schemaVersion: 1,
      stage: 'design',
      ok: hasError ? false : mapped.ok,
      applicable: mapped.applicable,
      reused: hasError ? false : Boolean(mapped.reused),
      status: hasError ? 'ERROR' : mapped.status,
      jiraKey: args.key || spec.jiraKey || materials?.key || '',
      requirementName: spec.requirementName || materials?.requirementName || '',
      conclusion: hasError ? '' : spec.conclusion || '',
      failedItemCount: 0,
      pendingItemCount: Number(spec.pending?.total || 0),
      deliverableVersion: String(spec.deliverableVersion || '1.0').replace(/^v/, ''),
      htmlPath: '',
      planPath: spec.planPath || '',
      xmindPath: spec.xmindPath || '',
      attachmentIds: uploaded.map((file) => String(file.id || '')).filter(Boolean),
      attachmentId: uploaded[0] ? String(uploaded[0].id || '') : '',
      commentId: String(upload?.comment?.commentId || comment?.commentId || ''),
      materialAttachmentIds: ids,
      gateStatus: spec.gate?.status || '',
      gateOpenCount: Number(spec.gate?.openCount || 0),
      gateStale: Boolean(spec.gate?.stale),
      reason: hasError ? String(args.error) : spec.reason || '',
      nextCommand: '',
      warnings: [],
    };
  } else {
    const specMaterialIds = (spec?.materials || []).map((material) => String(material.id)).filter(Boolean);
    result = {
      schemaVersion: 1,
      stage,
      ok: !hasError,
      applicable: true,
      status: hasError ? 'ERROR' : 'OK',
      jiraKey: args.key || materials?.key || spec?.jiraKey || '',
      requirementName: args.name || materials?.requirementName || spec?.requirementName || '',
      conclusion: args.conclusion || spec?.conclusion || '',
      failedItemCount: Number(args.failed ?? (spec?.failures?.length || 0)),
      pendingItemCount: 0,
      deliverableVersion: String(args.version || spec?.deliverableVersion || '1.0').replace(/^v/, ''),
      htmlPath: args.html || upload?.files?.[0]?.path || '',
      attachmentId: args.attachment || upload?.files?.[0]?.id || '',
      commentId: args.comment || comment?.commentId || '',
      materialAttachmentIds: specMaterialIds.length
        ? specMaterialIds
        : materials
          ? effectiveIds(materials).filter((id) => id !== DESCRIPTION_ID)
          : [],
      nextCommand: args['next-command'] || (args.key ? `/qa-design ${args.key}` : ''),
      warnings: [],
    };
  }

  if (hasError) {
    result.error = String(args.error);
    result.reason = String(args.error);
  }
  await writeResult(workspace, runId, result);
  printResult(result);
  if (hasError) return;
}

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: parseArgs(process.argv.slice(2)).stage || '', script: 'result' }),
);
