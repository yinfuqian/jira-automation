#!/usr/bin/env node
/**
 * 阶段二/三前置定位与计划：
 * - 阶段二：门禁只读取、不阻断；计算版本、是否跳过、待生成版本；
 * - 阶段三：测试方案与 XMind 必须存在，且有人工复核证据。
 */

import { existsSync } from 'node:fs';
import { readFile, readdir } from 'node:fs/promises';
import path from 'node:path';

import { EXIT, QaError, boolArg, checkNode, loadCredentials, log, parseArgs, parseIssueKey, resolveWorkspace } from './lib/env.mjs';
import { readJson, sha256 } from './lib/fs-safe.mjs';
import { attachmentList, JiraClient } from './lib/jira.mjs';
import { findGateRoots, resolveGate } from './lib/gate-evidence.mjs';
import { DESCRIPTION_ID, effectiveIds, parseMaterialLine, sameIdSet } from './lib/material-set.mjs';
import { handleFatal } from './lib/fatal.mjs';

const STAGE2_TITLE = /^h3\.\s*\*{0,2}\s*QA-测试方案结果v(\d+\.\d+)/i;
const REVIEW_TITLE = /^h3\.\s*\*{0,2}\s*QA-测试方案复核通过v(\d+\.\d+)/i;

function versionOf(file, kind) {
  const pattern = kind === 'plan' ? /-测试方案-v(\d+)\.(\d+)\.md$/i : /-测试用例-v(\d+)\.(\d+)\.xmind$/i;
  const match = path.basename(file).match(pattern);
  return match ? { major: Number(match[1]), minor: Number(match[2]), text: `${Number(match[1])}.${Number(match[2])}` } : null;
}

function maxVersion(list, kind) {
  let max = null;
  for (const item of list) {
    const version = versionOf(item.file || item.filename || item, kind);
    if (!version) continue;
    if (!max || version.major > max.major || (version.major === max.major && version.minor > max.minor)) {
      max = version;
    }
  }
  return max;
}

function nextVersion(max) {
  if (!max) return '1.0';
  return `${max.major}.${max.minor + 1}`;
}

async function listFiles(dir, filter) {
  if (!existsSync(dir)) return [];
  const entries = await readdir(dir, { withFileTypes: true });
  return entries.filter((entry) => entry.isFile() && filter(entry.name)).map((entry) => path.join(dir, entry.name));
}

function pickLatest(files, kind, { strict = false } = {}) {
  const parsed = files
    .map((file) => ({ file, version: versionOf(file, kind) }))
    .filter((item) => item.version)
    .sort((a, b) => (a.version.major === b.version.major ? b.version.minor - a.version.minor : b.version.major - a.version.major));
  if (!parsed.length) return null;
  if (strict && parsed.length > 1 && parsed[0].version.text === parsed[1].version.text) {
    throw new QaError(`存在多个同版本交付物，无法唯一确定：${parsed[0].file}、${parsed[1].file}`, EXIT.PRECONDITION);
  }
  return parsed[0];
}

function lastMaterialLine(body) {
  return String(body || '')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.includes('材料版本：'))
    .pop() || '';
}

function parseStage2Comments(comments) {
  let previous = null;
  const reviewedVersions = new Set();
  for (const comment of comments || []) {
    const body = String(comment.body || '').trimStart();
    const stageMatch = STAGE2_TITLE.exec(body);
    if (stageMatch) {
      const line = lastMaterialLine(body);
      const { ids, hasDescription } = parseMaterialLine(line);
      previous = {
        version: stageMatch[1],
        materialIds: ids,
        hasDescription,
        materialLine: line,
        commentId: String(comment.id),
      };
      continue;
    }
    const reviewMatch = REVIEW_TITLE.exec(body);
    if (reviewMatch) reviewedVersions.add(reviewMatch[1]);
  }
  return { previous, reviewedVersions };
}

async function localReview(workspace, key) {
  const markerPath = path.join(workspace, '.qa', 'reviews', `${key}-design.json`);
  if (!existsSync(markerPath)) return { markerPath, marker: null };
  return { markerPath, marker: await readJson(markerPath) };
}

function reviewVersionFromMarker(marker) {
  return marker?.plan ? (versionOf(marker.plan, 'plan')?.text || '') : '';
}

async function main() {
  checkNode();
  const argv = process.argv.slice(2);
  const args = parseArgs(argv);
  const key = parseIssueKey(args.key || args._[0]);
  const workspace = resolveWorkspace(args.workspace);
  const stage = args.stage === 'cases' ? 'cases' : 'design';
  const warnings = [];

  let materials = null;
  let currentIds = [];
  let currentHasDescription = false;
  if (args.materials) {
    materials = await readJson(args.materials);
    const effective = effectiveIds(materials);
    currentIds = effective.filter((id) => id !== DESCRIPTION_ID);
    currentHasDescription = effective.includes(DESCRIPTION_ID);
  }

  const scope = String(args.scope || materials?.scope || '');
  let roots = scope
    ? [path.join(workspace, 'outputs', ...scope.split('/'), key)]
    : findGateRoots(workspace, key, '');
  if (stage === 'design') {
    if (roots.length === 0) roots = [path.join(workspace, 'outputs', key)];
    if (roots.length > 1) {
      throw new QaError(`存在多个 ${key} 交付目录，请用 --scope 指定：${roots.join('、')}`, EXIT.PRECONDITION);
    }
  } else {
    if (roots.length === 0) throw new QaError(`找不到 ${key} 的交付目录（outputs/**/${key}）。`, EXIT.PRECONDITION);
    if (roots.length > 1) throw new QaError(`存在多个 ${key} 交付目录，请用 --scope 指定：${roots.join('、')}`, EXIT.PRECONDITION);
  }
  const root = roots[0];

  const planFiles = await listFiles(path.join(root, 'test-plans'), (name) => name.includes('测试方案-v') && name.endsWith('.md'));
  const xmindFiles = await listFiles(path.join(root, 'test-cases'), (name) => name.includes('测试用例-v') && name.endsWith('.xmind'));
  const latestPlan = pickLatest(planFiles, 'plan', { strict: stage === 'cases' });
  const latestXmind = pickLatest(xmindFiles, 'xmind', { strict: stage === 'cases' });

  let comments = [];
  let attachments = [];
  const creds = loadCredentials(process.env, { required: false });
  if (creds.baseUrl && creds.token) {
    try {
      const client = new JiraClient(creds);
      const issue = await client.getIssue(key, { comments: false });
      attachments = attachmentList(issue);
      comments = await client.getComments(key);
    } catch (error) {
      warnings.push(`Jira 读取失败：${error.message}`);
    }
  } else {
    warnings.push('缺少 Jira 凭据，门禁/复核证据仅使用本地文件');
  }

  const gate = await resolveGate({
    workspace,
    key,
    scope,
    currentIds,
    currentHasDescription,
    comments,
    allowJira: true,
  });

  const planMax = maxVersion(
    [...planFiles.map((file) => ({ file })), ...attachments.filter((att) => /-测试方案-v\d+\.\d+\.md$/i.test(att.filename))],
    'plan',
  );
  const xmindMax = maxVersion(
    [...xmindFiles.map((file) => ({ file })), ...attachments.filter((att) => /-测试用例-v\d+\.\d+\.xmind$/i.test(att.filename))],
    'xmind',
  );
  const max =
    !planMax ? xmindMax : !xmindMax ? planMax : planMax.major === xmindMax.major && planMax.minor >= xmindMax.minor ? planMax : xmindMax;
  const next = nextVersion(max);

  const force = boolArg(args, 'force');
  const { previous, reviewedVersions } = parseStage2Comments(comments);
  const { marker, markerPath } = await localReview(workspace, key);
  const markerVersion = reviewVersionFromMarker(marker);
  const reviewed = Boolean(
    (previous && (reviewedVersions.has(previous.version) || markerVersion === previous.version)) ||
      (markerVersion && (!previous || markerVersion === previous.version)),
  );

  // 线上每次可能是空工作区，产物存在性以 Jira 附件为准，本地文件只作补充。
  const hasArtifact = (version, kind) => {
    const safe = String(version).replace(/\./g, '\\.');
    const pattern = kind === 'plan'
      ? new RegExp(`-测试方案-v${safe}\\.md$`, 'i')
      : new RegExp(`-测试用例-v${safe}\\.xmind$`, 'i');
    if (attachments.some((att) => pattern.test(String(att.filename || '')))) return true;
    const local = kind === 'plan' ? planFiles : xmindFiles;
    return local.some((item) => pattern.test(String(item.file || item)));
  };

  const sameMaterials = previous
    ? sameIdSet(currentIds, previous.materialIds, {
        hasDescriptionA: currentHasDescription,
        hasDescriptionB: previous.hasDescription,
      })
    : false;
  const artifactsPresent = previous
    ? hasArtifact(previous.version, 'plan') && hasArtifact(previous.version, 'xmind')
    : false;
  const skip = Boolean(previous && sameMaterials && artifactsPresent && !force);

  let skipReason = '';
  if (!previous) skipReason = '没有上一次测试方案运行记录，生成新版本';
  else if (!sameMaterials) skipReason = '材料已变化，需要生成新版本';
  else if (!artifactsPresent) skipReason = `v${previous.version} 的方案或 XMind 附件缺失，需要重新生成`;
  else if (force) skipReason = '显式 --force，强制重新生成';
  else skipReason = `材料未变化且 v${previous.version} 产物齐备，复用上一版，不再生成`;

  // 门禁只读取、用于判定本阶段是否适用，不在这里中断流程。
  let blocked = false;
  let blockReason = '';
  if (gate.status === '未找到') {
    blocked = true;
    blockReason = '未找到阶段一需求准入门禁结论';
  } else if (gate.stale) {
    blocked = true;
    blockReason = '需求准入门禁结论已过期：材料附件在门禁结论后发生变化';
  } else if (String(gate.conclusion || '') !== '通过') {
    blocked = true;
    blockReason = `阶段一结论：${gate.conclusion || '未找到'}${gate.openCount ? `（${gate.openCount} 项未关闭）` : ''}`;
  }

  const review = stage === 'cases' ? await checkCasesReview({ workspace, key, latestPlan, latestXmind, markerPath, marker, reviewedVersions }) : null;

  const out = {
    ok: true,
    key,
    stage,
    root,
    scope,
    gate: {
      status: gate.status,
      conclusion: gate.conclusion,
      openCount: gate.openCount,
      stale: gate.stale,
      source: gate.source,
      version: gate.version,
      materialMatch: gate.materialMatch,
      currentIds,
      hasDescription: currentHasDescription,
    },
    materials: {
      file: args.materials || '',
      ids: currentIds,
      hasDescription: currentHasDescription,
    },
    plan: latestPlan ? latestPlan.file : '',
    xmind: latestXmind ? latestXmind.file : '',
    versions: {
      plan: planMax?.text || '',
      xmind: xmindMax?.text || '',
      max: max?.text || '',
      next,
    },
    previous: previous
      ? {
          version: previous.version,
          materialIds: previous.materialIds,
          hasDescription: previous.hasDescription,
          commentId: previous.commentId,
          reviewed: reviewedVersions.has(previous.version) || markerVersion === previous.version,
        }
      : null,
    review,
    design: {
      skip,
      reuse: skip,
      version: skip && previous ? previous.version : next,
      previousVersion: previous?.version || '',
      reason: skipReason,
      blocked,
      blockReason,
      force,
      reviewed,
      reviewedVersions: [...reviewedVersions],
      marker: markerPath,
    },
    warnings,
  };

  if (args.out) {
    const { writeJson } = await import('./lib/fs-safe.mjs');
    await writeJson(args.out, out);
  }
  process.stdout.write(`${JSON.stringify(out)}\n`);
}

async function checkCasesReview({ workspace, key, latestPlan, latestXmind, markerPath, marker, reviewedVersions }) {
  if (!latestPlan || !latestXmind) {
    throw new QaError('阶段三需要人工复核后的测试方案与人工审核后的 XMind。', EXIT.PRECONDITION);
  }
  if (marker) {
    const planHash = sha256(await readFile(latestPlan.file, 'utf8'));
    const xmindHash = sha256(await readFile(latestXmind.file));
    if (marker.planSha256 !== planHash || marker.xmindSha256 !== xmindHash) {
      throw new QaError('方案或 XMind 在人工复核后发生变化，请重新复核并更新标记。', EXIT.PRECONDITION);
    }
    return { reviewed: true, source: 'local-marker', version: reviewVersionFromMarker(marker) };
  }
  const planVersion = versionOf(latestPlan.file, 'plan')?.text || '';
  if (reviewedVersions.has(planVersion)) {
    return { reviewed: true, source: 'jira-comment', version: planVersion };
  }
  throw new QaError('缺少人工复核标记：请先写入 Jira 复核评论 QA-测试方案复核通过v<版本>，或运行 mark-reviewed.mjs。', EXIT.PRECONDITION);
}

main().catch((error) =>
  handleFatal(error, {
    argv: process.argv.slice(2),
    stage: (parseArgs(process.argv.slice(2)).stage || 'design'),
    comment: parseArgs(process.argv.slice(2)).stage !== 'cases',
    script: 'locate',
  }),
);
