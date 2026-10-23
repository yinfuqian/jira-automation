#!/usr/bin/env node
/**
 * cases.json → Excel + Xray CSV（10 列，UTF-8 BOM）。
 * Excel 由 Python 3 + openpyxl 生成；CSV 由 Node 生成。
 */

import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { EXIT, QaError, checkNode, parseArgs, parseIssueKey, resolveWorkspace } from './lib/env.mjs';
import { ensureDir, readJson, safeFilename, writeFileAtomic, writeJson } from './lib/fs-safe.mjs';

const PRIORITY = { P0: 'Highest', P1: 'High', P2: 'Medium', P3: 'Low' };
const HEADERS = [
  'Summary',
  'Description',
  'Test Repository Path',
  'Priority',
  'Labels',
  'Test Steps',
  'Data',
  'Expected Result',
  'External ID',
  'Requirements',
];

function csvCell(value) {
  return `"${String(value ?? '').replace(/"/g, '""')}"`;
}

function descriptionOf(c) {
  const parts = [
    `用例编号：${c.id}`,
    `模块：${c.module}`,
    `是否适合自动化：${c.automation || ''}`,
    '',
    '前置条件',
    c.precondition || '',
  ];
  if (c.remark) parts.push('', '补充说明', c.remark);
  return parts.join('\n');
}

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const workspace = resolveWorkspace(args.workspace);
  const casesPath = args.cases;
  if (!casesPath) {
    process.stderr.write('[build-cases][ERR] 缺少 --cases <cases.json>。\n');
    process.exit(EXIT.USAGE);
  }
  const data = await readJson(casesPath);
  const key = parseIssueKey(args.key || data.jiraKey);
  const cases = data.cases || [];
  if (cases.length === 0) throw new QaError('cases.json 中没有用例。', EXIT.VALIDATION);
  for (const c of cases) {
    for (const field of ['id', 'module', 'title', 'precondition', 'steps', 'expected', 'priority', 'type', 'requirements']) {
      if (!String(c[field] || '').trim()) throw new QaError(`用例 ${c.id || '(未知)'} 缺少 ${field}。`, EXIT.VALIDATION);
    }
    if (!PRIORITY[c.priority]) throw new QaError(`用例 ${c.id} 优先级非法：${c.priority}`, EXIT.VALIDATION);
  }
  const name = safeFilename(args.name || data.requirementName || '需求', '需求');
  const scope = args.scope || data.scope || '';
  const outDir = args['out-dir']
    ? path.resolve(workspace, args['out-dir'])
    : path.join(workspace, 'outputs', ...(scope ? String(scope).split('/') : []), key, 'test-cases');
  await ensureDir(outDir);
  const csvPath = path.join(outDir, `${key}-${name}-Jira导入.csv`);
  const xlsxPath = path.join(outDir, `${key}-${name}-测试用例.xlsx`);

  const rows = [HEADERS.join(',')];
  for (const c of cases) {
    rows.push(
      [
        csvCell(c.title),
        csvCell(descriptionOf(c)),
        csvCell(c.module),
        csvCell(PRIORITY[c.priority]),
        csvCell(c.type),
        csvCell(c.steps),
        csvCell(c.testData || ''),
        csvCell(c.expected),
        csvCell(c.id),
        csvCell(c.requirements || key),
      ].join(','),
    );
  }
  await writeFileAtomic(csvPath, `﻿${rows.join('\r\n')}\r\n`);

  const py = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
  const pyScript = path.join(path.dirname(fileURLToPath(import.meta.url)), 'build-xlsx.py');
  const res = spawnSync(py, [pyScript, casesPath, xlsxPath], { encoding: 'utf8' });
  if (res.status !== 0) {
    process.stderr.write(
      `[build-cases][ERR] Excel 生成失败。需要 Python 3 + openpyxl。\n${res.stderr || res.stdout || ''}\n`,
    );
    process.exit(EXIT.VALIDATION);
  }

  const summary = { ok: true, key, name, csvPath, xlsxPath, cases: cases.length };
  await writeJson(path.join(outDir, '.build.json'), summary);
  process.stdout.write(`CSV=${csvPath}\nXLSX=${xlsxPath}\nCASES=${cases.length}\n`);
}

main().catch((error) => {
  const code = error instanceof QaError ? error.code : EXIT.INTERNAL;
  process.stderr.write(`[build-cases][ERR] ${error.message}\n`);
  process.exit(code);
});
