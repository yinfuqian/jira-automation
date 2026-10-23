#!/usr/bin/env node
/**
 * 阶段二/三前置定位与门禁：
 * - 找到唯一需求根目录；
 * - 阶段二：最新阶段一结论必须为通过，且材料附件未变化；
 * - 阶段三：最新方案与 XMind 必须存在，且人工复核标记的哈希匹配。
 */

import { existsSync, readdirSync } from 'node:fs';
import { readFile, readdir } from 'node:fs/promises';
import path from 'node:path';

import { EXIT, QaError, checkNode, parseArgs, parseIssueKey, resolveWorkspace } from './lib/env.mjs';
import { readJson, readText, sha256 } from './lib/fs-safe.mjs';
import { pickLatest } from './lib/versions.mjs';

async function listFiles(dir, filter) {
  if (!existsSync(dir)) return [];
  const entries = await readdir(dir, { withFileTypes: true });
  return entries.filter((e) => e.isFile() && filter(e.name)).map((e) => path.join(dir, e.name));
}

function candidateRoots(workspace, key) {
  const roots = [];
  const direct = path.join(workspace, 'outputs', key);
  if (existsSync(direct)) roots.push(direct);
  const outputs = path.join(workspace, 'outputs');
  if (existsSync(outputs)) {
    for (const entry of readdirSync(outputs, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      const candidate = path.join(outputs, entry.name, key);
      if (existsSync(candidate)) roots.push(candidate);
    }
  }
  return [...new Set(roots)];
}

function requireOneRoot(workspace, key, scope) {
  if (scope) {
    const root = path.join(workspace, 'outputs', ...scope.split('/'), key);
    if (!existsSync(root)) throw new QaError(`指定 scope 下没有需求目录：${root}`, EXIT.PRECONDITION);
    return root;
  }
  const roots = candidateRoots(workspace, key);
  if (roots.length === 0) throw new QaError(`找不到 ${key} 的交付目录（outputs/**/${key}）。`, EXIT.PRECONDITION);
  if (roots.length > 1) {
    throw new QaError(`存在多个 ${key} 交付目录，请用 --scope 指定：${roots.join('、')}`, EXIT.PRECONDITION);
  }
  return roots[0];
}

async function latestGate(root, key) {
  const dir = path.join(root, 'risk-analysis');
  const files = await listFiles(dir, (n) => n.endsWith('.html') && n.includes('-需求准入门禁清单-v'));
  if (files.length === 0) return null;
  const latest = pickLatest(files);
  if (!latest) return null;
  const specPath = latest.file.replace(/.html$/i, '.spec.json');
  const spec = existsSync(specPath) ? await readJson(specPath) : null;
  return { html: latest.file, specPath: existsSync(specPath) ? specPath : '', spec };
}

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const key = parseIssueKey(args.key || args._[0]);
  const workspace = resolveWorkspace(args.workspace);
  const stage = args.stage || 'design';
  const root = requireOneRoot(workspace, key, args.scope);

  const gate = await latestGate(root, key);
  if (!gate) throw new QaError('缺少阶段一需求准入门禁清单，请先执行 /review-jira-qa。', EXIT.PRECONDITION);
  if (!gate.spec) throw new QaError(`阶段一清单缺少 spec 文件：${gate.html}`, EXIT.PRECONDITION);
  if (gate.spec.conclusion !== '通过' || (gate.spec.failures || []).length > 0) {
    throw new QaError(`阶段一结论为「${gate.spec.conclusion}」，仍有未关闭项，不能进入阶段二。`, EXIT.PRECONDITION);
  }

  if (args.materials) {
    const materials = await readJson(args.materials);
    const current = (materials.materials || []).filter((m) => m.role !== 'excluded').map((m) => String(m.id)).sort();
    const recorded = (gate.spec.materials || []).map((m) => String(m.id)).sort();
    if (JSON.stringify(current) !== JSON.stringify(recorded)) {
      throw new QaError('阶段一记录的材料附件与当前 Jira 附件不一致，请重跑 /review-jira-qa。', EXIT.PRECONDITION);
    }
  }

  const planFiles = await listFiles(path.join(root, 'test-plans'), (n) => n.includes('测试方案-v') && n.endsWith('.md'));
  const xmindFiles = await listFiles(path.join(root, 'test-cases'), (n) => n.includes('测试用例-v') && n.endsWith('.xmind'));
  const latestPlan = pickLatest(planFiles);
  const latestXmind = pickLatest(xmindFiles);

  let review = null;
  if (stage === 'cases') {
    if (!latestPlan || !latestXmind) {
      throw new QaError('阶段三需要人工复核后的测试方案与人工审核后的 XMind。', EXIT.PRECONDITION);
    }
    const markerPath = path.join(workspace, '.qa', 'reviews', `${key}-design.json`);
    if (!existsSync(markerPath) && args.reviewed !== true) {
      throw new QaError('缺少人工复核标记，请先运行 mark-reviewed.mjs 或显式 --reviewed。', EXIT.PRECONDITION);
    }
    if (existsSync(markerPath)) {
      review = await readJson(markerPath);
      const planHash = sha256(await readText(latestPlan.file));
      const xmindHash = sha256(await readFile(latestXmind.file));
      if (review.planSha256 !== planHash || review.xmindSha256 !== xmindHash) {
        throw new QaError('方案或 XMind 在人工复核后发生变化，请重新复核并更新标记。', EXIT.PRECONDITION);
      }
    }
  }

  const out = {
    ok: true,
    key,
    stage,
    root,
    gate: { html: gate.html, spec: gate.specPath, conclusion: gate.spec.conclusion, version: gate.spec.deliverableVersion },
    plan: latestPlan ? latestPlan.file : '',
    xmind: latestXmind ? latestXmind.file : '',
    reviewed: Boolean(review),
  };
  process.stdout.write(`${JSON.stringify(out)}\n`);
}

main().catch((error) => {
  const code = error instanceof QaError ? error.code : EXIT.INTERNAL;
  process.stderr.write(`[locate][ERR] ${error.message}\n`);
  process.exit(code);
});
