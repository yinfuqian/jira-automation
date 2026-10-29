#!/usr/bin/env node
/**
 * 校验 XMind 结构：默认命名空间、叶子、notes 四段、优先级标记、标题前缀。
 */

import { readFile } from 'node:fs/promises';

import { EXIT, QaError, checkNode, parseArgs } from './lib/env.mjs';
import { parseXMind, walkLeaves } from './lib/xmind.mjs';
import { handleFatal } from './lib/fatal.mjs';

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const file = args.xmind || args._[0];
  if (!file) {
    process.stderr.write('[validate-xmind][ERR] 缺少 --xmind <file>。\n');
    throw new QaError('参数或配置错误', EXIT.USAGE);
  }
  const root = parseXMind(await readFile(file));
  const leaves = walkLeaves(root);
  const errors = [];
  if (!root.title) errors.push('根 topic 缺少标题');
  if (leaves.length === 0) errors.push('没有叶子节点');
  const formal = leaves.filter((l) => !l.title.startsWith('【待确认】'));
  for (const leaf of leaves) {
    if (!/【.+】/.test(leaf.title)) errors.push(`叶子标题缺少【模块名称】：${leaf.title}`);
    for (const section of ['用例属性', '前置条件', '测试步骤', '预期结果']) {
      if (!String(leaf.notes || '').includes(section)) errors.push(`notes 缺少「${section}」：${leaf.title}`);
    }
    if (!/^priority-[1-4]$/.test(leaf.priority || '')) errors.push(`缺少任务优先级标记：${leaf.title}`);
    const numbered = (text) => (String(text || '').match(/^\s*\d+[.、]/gm) || []).length;
    if (numbered(leaf.notes) === 0) errors.push(`notes 中没有连续编号段落：${leaf.title}`);
  }
  const out = {
    ok: errors.length === 0,
    root: root.title,
    leaves: leaves.length,
    formalLeaves: formal.length,
    pendingLeaves: leaves.length - formal.length,
    errors,
  };
  process.stdout.write(`${JSON.stringify(out)}\n`);
  process.stdout.write(`PENDING_LEAVES=${out.pendingLeaves}\n`);
  if (errors.length) throw new QaError(`XMind 校验失败：${errors.join('；')}`, EXIT.VALIDATION);
}

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: "design", script: "validate-xmind" }),
);
