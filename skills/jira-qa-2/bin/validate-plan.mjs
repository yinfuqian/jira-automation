#!/usr/bin/env node
/**
 * 测试方案结构校验：文件名前缀与必备章节。
 */

import path from 'node:path';

import { EXIT, QaError, checkNode, parseArgs } from './lib/env.mjs';
import { readText } from './lib/fs-safe.mjs';
import { handleFatal } from './lib/fatal.mjs';

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
  '待测试点评审确认项',
];

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const plan = args.plan || args._[0];
  const key = args.key || '';
  if (!plan) {
    process.stderr.write('[validate-plan][ERR] 缺少 --plan <file>。\n');
    throw new QaError('参数或配置错误', EXIT.USAGE);
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
  if (errors.length) throw new QaError(`测试方案校验失败：${errors.join('；')}`, EXIT.VALIDATION);
}

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: "design", script: "validate-plan" }),
);
