#!/usr/bin/env node
/**
 * 同版本材料复用：当前需求材料顶层附件 ID 集合与上一版门禁一致时，
 * 直接复用上一版结论，不重新分析、不新增附件/评论。
 */

import path from 'node:path';

import { EXIT, QaError, boolArg, checkNode, loadCredentials, log, parseArgs, parseIssueKey, resolveWorkspace } from './lib/env.mjs';
import { JiraClient, attachmentList } from './lib/jira.mjs';
import { readJson, writeJson } from './lib/fs-safe.mjs';
import { makeRunId, printResult } from './lib/result.mjs';
import { effectiveIds, sameIdSet, DESCRIPTION_ID } from './lib/material-set.mjs';
import { latestGateComment } from './lib/gate-evidence.mjs';
import { handleFatal } from './lib/fatal.mjs';

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

  const effective = effectiveIds(materials);
  const currentIds = effective.filter((id) => id !== DESCRIPTION_ID);
  const currentHasDescription = effective.includes(DESCRIPTION_ID);

  const client = new JiraClient(loadCredentials());
  const warnings = [];
  let comments = [];
  try {
    comments = await client.getComments(key);
  } catch (error) {
    warnings.push(`读取 Jira 评论失败，降级为全量分析：${error.message}`);
  }
  const previous = latestGateComment(comments);
  const gate = previous?.gate || null;

  let reuse = false;
  let previousVersion = '';
  let conclusion = '';
  let previousIds = [];
  let previousHasDescription = false;
  let reason = '没有上一版门禁结果';
  if (gate) {
    previousVersion = gate.version || '';
    conclusion = gate.conclusion || '';
    previousIds = gate.materialIds || [];
    previousHasDescription = Boolean(gate.hasDescription);
    const same = sameIdSet(currentIds, previousIds, {
      hasDescriptionA: currentHasDescription,
      hasDescriptionB: previousHasDescription,
    });
    reuse = same && currentIds.length + (currentHasDescription ? 1 : 0) > 0;
    reason = same ? '材料附件 ID 集合一致，复用上一版结论' : '材料附件 ID 集合不一致，需要重新分析';
  }

  // 复用必须指得到上一版清单附件；附件被删时降级为重新生成。校验失败不改判，只记 warning。
  if (reuse) {
    try {
      const issue = await client.getIssue(key, { comments: false });
      const hasGateHtml = attachmentList(issue).some((att) => /-需求准入门禁清单-v\d+\.\d+\.html$/i.test(att.filename));
      if (!hasGateHtml) {
        reuse = false;
        reason = '上一版需求准入门禁清单附件已不存在，需重新生成';
      }
    } catch (error) {
      warnings.push(`校验门禁附件失败：${error.message}`);
    }
  }

  const failedItemCount = reuse && !force
    ? gate?.openCount || Number((conclusion.match(/(\d+)\s*项未关闭/) || [])[1] || 0)
    : 0;
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
    failedItemCount,
    deliverableVersion: previousVersion,
    htmlPath: '',
    attachmentId: '',
    commentId: previous ? String(previous.comment.id) : '',
    materialAttachmentIds: [...currentIds, ...(currentHasDescription ? [DESCRIPTION_ID] : [])],
    previousMaterialAttachmentIds: [...previousIds, ...(previousHasDescription ? [DESCRIPTION_ID] : [])],
    reason: force ? '显式 --force，重新分析' : reason,
    nextCommand: '',
    warnings,
  };

  await writeJson(path.join(workspace, '.qa', 'runs', runId, 'reuse.json'), result);
  log('reuse', `reuse=${result.reused} previous=${previousVersion || 'none'} reason=${result.reason}`);
  process.stdout.write(
    `REUSE=${result.reused}\nPREVIOUS_VERSION=${previousVersion}\nPREVIOUS_COMMENT_ID=${result.commentId}\nCONCLUSION=${result.conclusion}\n`,
  );
  if (result.reused) printResult(result);
}

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: 'risk-analysis', comment: false, script: 'reuse' }),
);
