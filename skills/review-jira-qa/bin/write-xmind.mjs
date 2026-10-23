#!/usr/bin/env node
/**
 * xmind.spec.json → XMind 2.0 文件。
 */

import path from 'node:path';

import { EXIT, QaError, boolArg, checkNode, parseArgs, resolveWorkspace } from './lib/env.mjs';
import { ensureDir, readJson, safeFilename, writeFileAtomic } from './lib/fs-safe.mjs';
import { buildXMind, walkLeaves } from './lib/xmind.mjs';
import { versionedName } from './lib/versions.mjs';
import { handleFatal } from './lib/fatal.mjs';

function validateSpec(spec) {
  if (!spec.root || !spec.root.title) throw new QaError('xmind.spec.json 缺少 root.title。', EXIT.VALIDATION);
  const leaves = walkLeaves(spec.root);
  if (leaves.length === 0) throw new QaError('xmind.spec.json 没有叶子节点。', EXIT.VALIDATION);
  for (const leaf of leaves) {
    if (!leaf.title) throw new QaError('存在空标题叶子。', EXIT.VALIDATION);
    if (!/【.+】/.test(leaf.title)) throw new QaError(`叶子标题缺少【模块名称】：${leaf.title}`, EXIT.VALIDATION);
    for (const section of ['用例属性', '前置条件', '测试步骤', '预期结果']) {
      if (!String(leaf.notes || '').includes(section)) {
        throw new QaError(`叶子 notes 缺少「${section}」：${leaf.title}`, EXIT.VALIDATION);
      }
    }
    if (!/^priority-[1-4]$/.test(leaf.priority || '')) {
      throw new QaError(`叶子缺少任务优先级标记（priority-1..4）：${leaf.title}`, EXIT.VALIDATION);
    }
  }
  return leaves;
}

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const workspace = resolveWorkspace(args.workspace);
  const specPath = args.spec || args._[0];
  if (!specPath) {
    process.stderr.write('[write-xmind][ERR] 缺少 --spec <xmind.spec.json>。\n');
    throw new QaError('参数或配置错误', EXIT.USAGE);
  }
  const spec = await readJson(specPath);
  const leaves = validateSpec(spec);
  if (boolArg(args, 'check-only')) {
    process.stdout.write(`LEAVES=${leaves.length}\nCHECK=OK\n`);
    return;
  }
  const version = String(args.version || spec.deliverableVersion || '1.0').replace(/^v/, '');
  const name = safeFilename(spec.requirementName || '需求', '需求');
  const filename = versionedName(spec.jiraKey, name, 'xmind', version);
  const out = args.out
    ? path.resolve(workspace, args.out)
    : path.join(workspace, 'outputs', ...(spec.scope ? String(spec.scope).split('/') : []), spec.jiraKey, 'test-cases', filename);
  await ensureDir(path.dirname(out));
  await writeFileAtomic(out, buildXMind(spec));
  process.stdout.write(`XMIND=${out}\nLEAVES=${leaves.length}\nVERSION=${version}\n`);
}

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: "design", comment: false, script: "write-xmind" }),
);
