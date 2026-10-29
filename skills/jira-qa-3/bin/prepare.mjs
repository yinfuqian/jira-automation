#!/usr/bin/env node
/**
 * 阶段三的前置编排：把多次脚本往返合并成一次调用，减少模型轮次。
 *
 * 阶段一：preflight → jira-read → jira-download → reuse → materials → coverage
 * 阶段二：preflight → jira-read → jira-download → locate → design-spec
 * 阶段三：preflight → jira-read → jira-download → fetch-artifacts → locate
 *
 * 幂等语义不变：材料未变时阶段一返回 REUSED（复用上一版结论），阶段二返回 REUSED（零写入）。
 * 本脚本只编排顺序与汇总结果，不改变任何单个脚本的判定规则。
 */

import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';

import { EXIT, QaError, checkNode, parseArgs, parseIssueKey, resolveWorkspace, log, skillDir } from './lib/env.mjs';
import { readJson } from './lib/fs-safe.mjs';
import { IGNORED_ROLES } from './lib/material-set.mjs';
import { handleFatal } from './lib/fatal.mjs';

const ROOT = skillDir();

function run(script, args) {
  const res = spawnSync(process.execPath, [path.join(ROOT, 'bin', script), ...args], {
    cwd: ROOT,
    env: process.env,
    encoding: 'utf8',
  });
  const stdout = String(res.stdout || '');
  const stderr = String(res.stderr || '');
  if (res.status !== 0) {
    throw new QaError(`${script} 执行失败（exit ${res.status}）：${(stderr || stdout).trim().slice(0, 400)}`, EXIT.INTERNAL);
  }
  return { stdout, stderr };
}

function valueOf(stdout, key) {
  const line = String(stdout)
    .split(/\r?\n/)
    .find((item) => item.startsWith(`${key}=`));
  return line ? line.slice(key.length + 1).trim() : '';
}

function jsonOf(stdout) {
  const idx = String(stdout).lastIndexOf('QA_RESULT_JSON');
  if (idx < 0) return null;
  const rest = stdout.slice(idx + 'QA_RESULT_JSON'.length).trim();
  const line = rest.split(/\r?\n/).find((item) => item.trim().startsWith('{'));
  try {
    return line ? JSON.parse(line) : null;
  } catch {
    return null;
  }
}

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const key = parseIssueKey(args.key || args._[0]);
  const workspace = resolveWorkspace(args.workspace);
  const rawStage = String(args.stage || '1');
  const stage = {
    1: 'risk-analysis',
    2: 'design',
    3: 'cases',
    'risk-analysis': 'risk-analysis',
    design: 'design',
    cases: 'cases',
  }[rawStage];
  if (!stage) throw new QaError('--stage 只支持 1（阶段一）、2（阶段二）或 3（阶段三）', EXIT.USAGE);
  // jira-qa-3 只负责阶段三：其它阶段由 review-jira-qa / jira-qa-2 处理，这里直接拒绝。
  if (stage !== 'cases') {
    throw new QaError('jira-qa-3 只执行阶段三，请使用 --stage 3。', EXIT.USAGE);
  }

  const common = ['--key', key, '--workspace', workspace];
  const out = {
    ok: true,
    stage,
    jiraKey: key,
    status: 'READY',
    runId: '',
    scope: '',
    requirementName: '',
    primaryId: '',
    primaryKind: '',
    materialCount: 0,
    descriptionChars: 0,
    suggestedVersion: '',
    suggestedDesignVersion: '',
    digest: '',
    coverageRequired: '',
    requiredAcs: 0,
    reused: false,
    conclusion: '',
    locate: '',
    designSpec: '',
    designStatus: '',
    designReused: false,
    fetch: '',
    plan: '',
    xmind: '',
    cases: '',
    review: null,
    gate: null,
    pending: null,
    reason: '',
    steps: [],
    warnings: [],
  };

  const started = Date.now();
  const mark = (name) => {
    out.steps.push({ name, ms: Date.now() - started });
    log('prepare', `${name} 完成（累计 ${out.steps[out.steps.length - 1].ms}ms）`);
  };

  const preflightStage = stage === 'design' ? '2' : stage === 'cases' ? '3' : '1';
  const pre = run('preflight.mjs', [...common, '--stage', preflightStage]);
  mark('preflight');
  void pre;

  // 阶段一需要上一版门禁评论做复评稳定性判断；保留最近 10 条评论。
  const read = run('jira-read.mjs', [...common, '--comments', '10']);
  const runId = valueOf(read.stdout, 'RUN_ID');
  if (!runId) throw new QaError('jira-read 未返回 RUN_ID', EXIT.INTERNAL);
  out.runId = runId;
  mark('jira-read');

  const runDir = path.join(workspace, '.qa', 'runs', runId);
  const materialsPath = path.join(runDir, 'materials.json');
  const stageArgs = stage === 'design' ? ['--stage', '2'] : stage === 'cases' ? ['--stage', '3'] : [];
  const download = run('jira-download.mjs', [...common, '--run-id', runId, ...stageArgs]);
  mark('jira-download');

  if (valueOf(download.stdout, 'SKIPPED')) {
    const result = await readJson(path.join(runDir, 'result.json')).catch(() => null);
    out.status = 'SKIPPED';
    out.ok = true;
    out.reason = result?.reason || '';
    out.commentId = result?.commentId || '';
    out.errorCode = result?.errorCode || valueOf(download.stdout, 'SKIPPED');
    process.stdout.write(`${JSON.stringify(out)}\n`);
    return;
  }

  const materials = await readJson(materialsPath);
  out.scope = materials.scope || '';
  out.requirementName = materials.requirementName || '';
  out.primaryId = String(materials.primaryId || '');
  out.primaryKind = String(materials.primaryKind || '');
  out.materialCount = (materials.materials || []).filter((item) => !IGNORED_ROLES.has(item.role)).length;
  out.descriptionChars = Number(materials.descriptionChars || 0);
  out.suggestedVersion = String(materials.suggestedDeliverableVersion || '');
  out.suggestedDesignVersion = String(materials.suggestedDesignVersion || '');

  if (stage === 'risk-analysis') {
    const reuse = run('reuse.mjs', [...common, '--materials', materialsPath, '--run-id', runId]);
    mark('reuse');
    if (valueOf(reuse.stdout, 'REUSE') === 'true') {
      out.status = 'REUSED';
      out.reused = true;
      out.conclusion = valueOf(reuse.stdout, 'CONCLUSION');
      out.suggestedVersion = valueOf(reuse.stdout, 'PREVIOUS_VERSION') || out.suggestedVersion;
      out.commentId = valueOf(reuse.stdout, 'PREVIOUS_COMMENT_ID');
      process.stdout.write(`${JSON.stringify(out)}\n`);
      return;
    }

    const digest = run('materials.mjs', [...common, '--run-id', runId]);
    out.digest = valueOf(digest.stdout, 'DIGEST');
    mark('materials');

    const coverage = run('coverage.mjs', ['--materials', materialsPath, '--workspace', workspace]);
    out.coverageRequired = valueOf(coverage.stdout, 'OUT');
    out.requiredAcs = Number(valueOf(coverage.stdout, 'REQUIRED_ACS') || 0);
    mark('coverage');
  } else if (stage === 'design') {
    const locateOut = path.join(runDir, 'locate.json');
    const locate = run('locate.mjs', [...common, '--stage', 'design', '--materials', materialsPath, '--out', locateOut]);
    out.locate = locateOut;
    mark('locate');

    const specOut = path.join(runDir, 'design.spec.json');
    const spec = run('design-spec.mjs', [
      ...common,
      '--run-id',
      runId,
      '--locate',
      locateOut,
      '--materials',
      materialsPath,
      '--out',
      specOut,
    ]);
    out.designSpec = specOut;
    out.designStatus = valueOf(spec.stdout, 'STATUS');
    out.pending = valueOf(spec.stdout, 'PENDING');
    const locateJson = await readJson(locateOut).catch(() => null);
    out.gate = locateJson?.gate || null;
    out.designReused = Boolean(locateJson?.design?.reuse);
    out.reason = String(locateJson?.design?.blockReason || locateJson?.design?.reason || '');
    out.status = out.designStatus === 'generated' ? 'READY' : out.designStatus === 'reused' ? 'REUSED' : 'BLOCKED';
    mark('design-spec');
  } else {
    // 空工作区里没有阶段二产物，先从 Jira 取回最新方案与 XMind，再交给 locate 校验人工复核。
    const fetch = run('fetch-artifacts.mjs', [
      ...common,
      '--run-id',
      runId,
      '--materials',
      materialsPath,
    ]);
    out.fetch = path.join(runDir, 'fetch.json');
    out.plan = valueOf(fetch.stdout, 'PLAN');
    out.xmind = valueOf(fetch.stdout, 'XMIND');
    mark('fetch-artifacts');

    const locateOut = path.join(runDir, 'locate-cases.json');
    const locate = run('locate.mjs', [
      ...common,
      '--stage',
      'cases',
      '--materials',
      materialsPath,
      '--out',
      locateOut,
    ]);
    mark('locate');

    // locate 的异常由 handleFatal 收敛为 exit 0，这里以产物文件是否存在区分 READY 与 BLOCKED。
    if (!existsSync(locateOut)) {
      const detail = String(locate.stderr || '')
        .split(/\r?\n/)
        .find((line) => line.includes('[ERR]')) || '阶段三前置条件未满足';
      out.ok = true;
      out.status = 'BLOCKED';
      out.reason = detail.replace(/^.*?\[ERR\]\s*/, '').trim();
      out.cases = '';
      process.stdout.write(`${JSON.stringify(out)}\n`);
      return;
    }

    const locateJson = await readJson(locateOut);
    out.cases = locateOut;
    out.plan = locateJson.plan || out.plan;
    out.xmind = locateJson.xmind || out.xmind;
    out.review = locateJson.review || null;
    out.gate = locateJson.gate || null;
    out.reason = String(locateJson.design?.reason || '');
    out.status = 'READY';
  }

  process.stdout.write(`${JSON.stringify(out)}\n`);
}

main().catch((error) =>
  // 本技能只负责阶段三，异常结果统一记在 cases 阶段。
  handleFatal(error, { argv: process.argv.slice(2), stage: 'cases', script: 'prepare' }),
);
