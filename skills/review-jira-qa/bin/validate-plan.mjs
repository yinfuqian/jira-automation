#!/usr/bin/env node
/**
 * 测试方案结构校验：文件名前缀与必备章节。
 */

import path from 'node:path';

import { EXIT, QaError, checkNode, parseArgs } from './lib/env.mjs';
import { readText } from './lib/fs-safe.mjs';

const REQUIRED = [
  '测试目标',
  '测试范围',
  '非测试范围',
  '测试策略',
  '测试重点',
  '测试环境',
  '测试数据',
  '入口标准',
  '出口标准',
  '风险',
];

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const plan = args.plan || args._[0];
  const key = args.key || '';
  if (!plan) {
    process.stderr.write('[validate-plan][ERR] 缺少 --plan <file>。\n');
    process.exit(EXIT.USAGE);
  }
  const base = path.basename(plan);
  const text = await readText(plan);
  const errors = [];
  if (key && !base.startsWith(`${key}-`)) errors.push(`文件名未以 ${key}- 开头`);
  if (!/-测试方案-v\d+\.\d+\.md$/.test(base)) errors.push('文件名不符合 <KEY>-<名称>-测试方案-v<版本>.md');
  for (const section of REQUIRED) {
    if (!text.includes(section)) errors.push(`缺少章节：${section}`);
  }
  process.stdout.write(`${JSON.stringify({ ok: errors.length === 0, plan, errors })}\n`);
  process.exit(errors.length ? EXIT.VALIDATION : EXIT.OK);
}

main().catch((error) => {
  const code = error instanceof QaError ? error.code : EXIT.INTERNAL;
  process.stderr.write(`[validate-plan][ERR] ${error.message}\n`);
  process.exit(code);
});
