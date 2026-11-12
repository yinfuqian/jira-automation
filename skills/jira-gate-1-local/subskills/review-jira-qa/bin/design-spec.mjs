#!/usr/bin/env node
/**
 * 组装阶段二运行规格 design.spec.json：
 * 门禁状态、待确认计数、版本、材料集合、产物路径。
 */

import path from 'node:path';

import { EXIT, QaError, checkNode, parseArgs, resolveWorkspace } from './lib/env.mjs';
import { readJson, writeJson } from './lib/fs-safe.mjs';
import { statusText } from './lib/gate-evidence.mjs';
import { DESCRIPTION_ID, effectiveIds } from './lib/material-set.mjs';
import { countPending } from './lib/pending.mjs';
import { walkLeaves } from './lib/xmind.mjs';
import { handleFatal } from './lib/fatal.mjs';

function prdVersionOf(materials) {
  const primary = (materials?.materials || []).find(
    (material) => String(material.key || material.id) === String(materials.primaryKey || materials.primaryId || ''),
  );
  if (!primary) return '';
  const match = /(?:^|[_\-\s])v?(\d+\.\d+)(?:[_\-\s.]|$)/i.exec(primary.filename || '');
  return match ? match[1] : '';
}

function commentTitleFor(status, version) {
  if (status === 'not-satisfied') return 'QA-测试方案不满足执行条件';
  return `QA-测试方案结果v${version}`;
}

/** 终态只由门禁与材料判定推导，不依赖调用方传参。 */
export function deriveStatus(locate) {
  const design = locate?.design || {};
  if (design.blocked) return 'not-satisfied';
  if (design.reuse) return 'reused';
  return 'generated';
}

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const workspace = resolveWorkspace(args.workspace);
  const key = args.key || '';
  const runId = args['run-id'] || 'manual';
  if (!args['locate']) throw new QaError('缺少 --locate <locate.json>。', EXIT.USAGE);
  if (!args.materials) throw new QaError('缺少 --materials <materials.json>。', EXIT.USAGE);
  const locate = await readJson(args.locate);
  const materials = await readJson(args.materials);
  const status = deriveStatus(locate);
  if (args.status && String(args.status) !== status) {
    process.stderr.write(`[design-spec][WARN] --status ${args.status} 与推导结果 ${status} 不一致，以推导结果为准\n`);
  }
  const version = String(args.version || locate.design?.version || '1.0').replace(/^v/, '');
  const planPath = args.plan ? path.resolve(workspace, args.plan) : '';
  const xmindPath = args.xmind ? path.resolve(workspace, args.xmind) : '';
  const planFile = planPath ? path.basename(planPath) : '';
  const xmindFile = xmindPath ? path.basename(xmindPath) : '';

  let leaveTitles = [];
  if (args['xmind-spec']) {
    try {
      const spec = await readJson(path.resolve(workspace, args['xmind-spec']));
      leaveTitles = walkLeaves(spec.root).map((leaf) => String(leaf.title || ''));
    } catch {
      leaveTitles = [];
    }
  }

  const gate = locate.gate || {};
  const gateForText = {
    conclusion: gate.conclusion || (gate.status === '通过' ? '通过' : ''),
    openCount: Number(gate.openCount || 0),
    stale: Boolean(gate.stale),
  };
  const gateStatusText = gate.status === '未找到' ? '未找到' : statusText(gateForText);
  const pending = countPending({ gate, materials, leaveTitles });
  const ids = effectiveIds(materials);
  const hasDescription = ids.includes(DESCRIPTION_ID);

  const conclusion =
    status === 'reused'
      ? '材料未变化，复用上一版'
      : status === 'not-satisfied'
        ? '不满足执行条件'
        : '已生成待复核';

  const out = {
    schemaVersion: 1,
    jiraKey: key || locate.key || materials.key || '',
    requirementName: materials.requirementName || '',
    scope: materials.scope || locate.scope || '',
    deliverableVersion: version,
    planFile,
    xmindFile,
    planPath,
    xmindPath,
    gate: {
      status: gate.status || '未找到',
      statusText: gateStatusText,
      conclusion: gate.conclusion || '',
      openCount: Number(gate.openCount || 0),
      stale: Boolean(gate.stale),
      source: gate.source || 'none',
      version: gate.version || '',
      materialMatch: gate.materialMatch ?? null,
    },
    pending,
    materials: ids,
    hasDescription,
    prdVersion: args['prd-version'] || prdVersionOf(materials),
    status,
    reused: status === 'reused',
    conclusion,
    reason: String(
      args.reason ||
        (status === 'not-satisfied' ? locate.design?.blockReason || locate.design?.reason : locate.design?.reason) ||
        '',
    ),
    commentTitle: commentTitleFor(status, version),
    runId,
  };

  const file = args.out
    ? path.resolve(workspace, args.out)
    : path.join(workspace, '.qa', 'runs', runId, 'design.spec.json');
  await writeJson(file, out);
  process.stdout.write(`DESIGN_SPEC=${file}\nSTATUS=${status}\nVERSION=${version}\nPENDING=${pending.total}\n`);
}

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: 'design', script: 'design-spec' }),
);
