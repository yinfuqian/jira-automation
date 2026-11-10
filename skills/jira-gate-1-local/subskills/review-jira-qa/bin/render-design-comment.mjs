#!/usr/bin/env node
/**
 * design.spec.json → 阶段二 Jira wiki 评论。
 * 成功评论只输出待确认总数，不列明细；明细在测试方案与 XMind 中。
 * 复用上一版（reused）不产出评论。
 */

import path from 'node:path';

import { EXIT, QaError, checkNode, parseArgs, resolveWorkspace } from './lib/env.mjs';
import { readJson, writeFileAtomic } from './lib/fs-safe.mjs';
import { DESCRIPTION_ID } from './lib/material-set.mjs';
import { handleFatal } from './lib/fatal.mjs';

function clean(value) {
  return String(value ?? '').replace(/\s+/g, ' ').trim();
}

function materialLine(spec) {
  const ids = (spec.materials || []).filter((id) => id !== DESCRIPTION_ID).join('、');
  const hasDescription = Boolean(spec.hasDescription) || (spec.materials || []).includes(DESCRIPTION_ID);
  const suffix = ids
    ? `（Jira 附件 ${ids}${hasDescription ? '；需求描述取自 Jira 字段' : ''}）`
    : hasDescription
      ? '（Jira 附件 无；需求描述取自 Jira 字段）'
      : '（Jira 附件 无）';
  return `材料版本：PRD${spec.prdVersion ? ` ${spec.prdVersion}` : ''}${suffix}`;
}

function render(spec) {
  const version = String(spec.deliverableVersion || '1.0').replace(/^v/, '');
  const gateText = spec.gate?.statusText || spec.gate?.status || '未找到';
  const lines = [];
  // 复用上一版时不写任何 Jira 内容：结果不变即零写入。
  if (spec.status === 'reused') return '';
  if (spec.status === 'not-satisfied') {
    lines.push('h3. QA-测试方案不满足执行条件', '');
    lines.push('*执行结论：不满足执行条件，本次未生成测试方案*');
    lines.push('阶段：design');
    lines.push(`原因：${clean(spec.reason) || '未提供'}`);
    if (spec.errorCode) lines.push(`标记：${spec.errorCode}`);
    lines.push(`阶段一：${gateText}`);
    lines.push('本次未修改阶段一需求准入门禁结论与评论。');
    return lines.join('\n');
  }

  lines.push(`h3. QA-测试方案结果v${version}`, '');
  lines.push('*执行结论：测试方案已生成，请人工复核结果*');
  lines.push(`阶段一：${gateText}`);
  lines.push(`影响测试方案的待确认项：${Number(spec.pending?.total || 0)} 项`);
  lines.push(
    `附件：{{${spec.planFile || ''}}}${spec.xmindFile ? ` {{${spec.xmindFile}}}` : ''}`,
  );
  lines.push('', materialLine(spec));
  return lines.join('\n');
}

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const workspace = resolveWorkspace(args.workspace);
  if (!args.spec) throw new QaError('缺少 --spec <design.spec.json>。', EXIT.USAGE);
  const spec = await readJson(path.resolve(workspace, args.spec));
  const body = render(spec);
  if (!body) {
    process.stdout.write('COMMENT=none\nSKIP_COMMENT=reused\n');
    return;
  }
  if (args.out) {
    const out = path.resolve(workspace, args.out);
    await writeFileAtomic(out, `${body}\n`);
    process.stdout.write(`COMMENT=${out}\n`);
  } else {
    process.stdout.write(`${body}\n`);
  }
}

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: 'design', script: 'render-design-comment' }),
);
