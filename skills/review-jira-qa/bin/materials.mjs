#!/usr/bin/env node
/**
 * 把 materials.json 汇总成给模型阅读的材料摘要。
 * 不改变原文；只做拼接、标注与截断（截断处显式声明）。
 */

import path from 'node:path';

import { EXIT, QaError, checkNode, log, parseArgs, resolveWorkspace } from './lib/env.mjs';
import { readText, writeFileAtomic, readJson } from './lib/fs-safe.mjs';

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const workspace = resolveWorkspace(args.workspace);
  const runId = args['run-id'];
  if (!runId) {
    process.stderr.write('[materials][ERR] 缺少 --run-id。\n');
    process.exit(EXIT.USAGE);
  }
  const runDir = path.join(workspace, '.qa', 'runs', runId);
  const materials = await readJson(path.join(runDir, 'materials.json'));
  const maxChars = Number(args['max-chars'] || 30000);
  const lines = [
    `# 需求材料摘要（${materials.key}）`,
    '',
    `- 工作区：${workspace}`,
    `- 主需求附件 ID：${materials.primaryId}`,
    `- 建议需求名称：${materials.requirementName}`,
    `- scope：${materials.scope || '(空)'}`,
    '',
    '## 材料清单',
    '',
    '| 附件 ID | 文件 | 角色 | 类型 | 可抽取字符 | 处理要求 |',
    '|---|---|---|---|---|---|',
  ];
  for (const m of materials.materials) {
    const requirement = m.needsRead
      ? '用 Read 工具直接查看原文件'
      : m.unreadable
        ? '抽取失败：必须用 Read 查看，仍不可读则记为材料不可读'
        : m.textChars > 0
          ? '阅读下方抽取正文'
          : '空文件或无可读正文，需确认';
    lines.push(`| ${m.id} | ${m.filename} | ${m.role} | ${m.kind} | ${m.textChars} | ${requirement} |`);
  }
  lines.push('', '## 抽取正文', '');
  for (const m of materials.materials) {
    if (m.role === 'excluded') continue;
    lines.push(`### ${m.filename}（附件 ${m.id}，${m.role}）`, '');
    if (m.needsRead) {
      lines.push(`> 该文件为 ${m.kind}，未抽取文本。请用 Read 工具打开：${m.localFile}`, '');
      continue;
    }
    if (!m.extractedPath) {
      lines.push('> 未抽取到正文。若该材料属于需求侧，必须用 Read 直接查看后再判定；仍不可读时记为材料不可读。', '');
      continue;
    }
    let text = await readText(m.extractedPath);
    if (text.length > maxChars) {
      text = `${text.slice(0, maxChars)}\n\n[已截断：原文共 ${text.length} 字符，仅显示前 ${maxChars} 字符]`;
    }
    lines.push(text, '');
  }
  const excluded = materials.materials.filter((m) => m.role === 'excluded');
  if (excluded.length) {
    lines.push('## 已排除（开发设计材料，不作为准入依据）', '');
    for (const m of excluded) lines.push(`- ${m.filename}（附件 ${m.id}）`);
    lines.push('');
  }
  const out = path.join(runDir, 'materials-digest.md');
  await writeFileAtomic(out, lines.join('\n'));
  log('materials', `digest=${out} chars=${lines.join('\n').length}`);
  process.stdout.write(`DIGEST=${out}\n`);
}

main().catch((error) => {
  const code = error instanceof QaError ? error.code : EXIT.INTERNAL;
  process.stderr.write(`[materials][ERR] ${error.message}\n`);
  process.exit(code);
});
