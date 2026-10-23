#!/usr/bin/env node
/**
 * 阶段三机械一致性校验：XMind 正式叶子 = Excel 行数 = CSV 行数；ID/Requirements/表头/Data 一致。
 */

import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

import { EXIT, QaError, checkNode, parseArgs, parseIssueKey } from './lib/env.mjs';
import { readJson } from './lib/fs-safe.mjs';
import { parseXMind, walkLeaves } from './lib/xmind.mjs';
import { handleFatal } from './lib/fatal.mjs';

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

function parseCsv(text) {
  const rows = [];
  let row = [];
  let cell = '';
  let quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') {
        cell += '"';
        i += 1;
      } else if (ch === '"') {
        quoted = false;
      } else {
        cell += ch;
      }
    } else if (ch === '"') {
      quoted = true;
    } else if (ch === ',') {
      row.push(cell);
      cell = '';
    } else if (ch === '\n') {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = '';
    } else if (ch !== '\r') {
      cell += ch;
    }
  }
  if (cell || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows.filter((r) => r.some((v) => String(v).trim() !== ''));
}

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const casesPath = args.cases;
  const xlsx = args.xlsx;
  const csv = args.csv;
  if (!casesPath || !xlsx || !csv) {
    process.stderr.write('[validate-cases][ERR] 需要 --cases <json> --xlsx <file> --csv <file>。\n');
    throw new QaError('参数或配置错误', EXIT.USAGE);
  }
  const data = await readJson(casesPath);
  const key = parseIssueKey(args.key || data.jiraKey);
  const cases = data.cases || [];
  const errors = [];

  const raw = await readFile(csv);
  if (!(raw[0] === 0xef && raw[1] === 0xbb && raw[2] === 0xbf)) errors.push('CSV 缺少 UTF-8 BOM');
  const rows = parseCsv(raw.toString('utf8').replace(/^﻿/, ''));
  if (rows.length === 0) errors.push('CSV 为空');
  else {
    const header = rows[0];
    if (JSON.stringify(header) !== JSON.stringify(HEADERS)) errors.push(`CSV 表头不符：${header.join(',')}`);
    const dataRows = rows.slice(1);
    if (dataRows.length !== cases.length) errors.push(`CSV 行数 ${dataRows.length} ≠ 用例数 ${cases.length}`);
    const idIndex = header.indexOf('External ID');
    const reqIndex = header.indexOf('Requirements');
    const dataIndex = header.indexOf('Data');
    for (let i = 0; i < cases.length; i += 1) {
      const row = dataRows[i];
      if (!row) continue;
      if (row[idIndex] !== cases[i].id) errors.push(`第 ${i + 1} 行 External ID 不是 ${cases[i].id}`);
      if (row[reqIndex] !== key) errors.push(`第 ${i + 1} 行 Requirements 不是 ${key}`);
      if (String(row[dataIndex] || '') !== String(cases[i].testData || '')) {
        errors.push(`第 ${i + 1} 行 Data 与 cases.json 测试数据不一致`);
      }
    }
  }

  const py = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
  const pyScript = path.join(path.dirname(fileURLToPath(import.meta.url)), 'read-xlsx.py');
  const res = spawnSync(py, [pyScript, xlsx], {
    encoding: 'utf8',
    timeout: Number(process.env.QA_PY_TIMEOUT_MS || 120000),
    maxBuffer: 10 * 1024 * 1024,
  });
  if (res.error || res.signal || res.status !== 0) {
    errors.push(`Excel 读取失败（需要 Python 3 + openpyxl）：${(res.error?.message || res.signal || res.stderr || '').trim()}`);
  } else {
    const info = JSON.parse(res.stdout);
    if (!info.sheets.includes('测试用例')) errors.push('Excel 缺少「测试用例」工作表');
    if (!info.sheets.includes('Xray导入映射')) errors.push('Excel 缺少「Xray导入映射」工作表');
    if (info.count !== cases.length) errors.push(`Excel 行数 ${info.count} ≠ 用例数 ${cases.length}`);
    if (info.headers.includes('Requirements') === false) errors.push('Excel 缺少 Requirements 列');
  }

  if (args.xmind) {
    const root = parseXMind(await readFile(args.xmind));
    const leaves = walkLeaves(root).filter((l) => !l.title.startsWith('【待确认】'));
    if (leaves.length !== cases.length) errors.push(`XMind 正式叶子 ${leaves.length} ≠ 用例数 ${cases.length}`);
  }

  const out = { ok: errors.length === 0, cases: cases.length, errors };
  process.stdout.write(`${JSON.stringify(out)}\n`);
  if (errors.length) throw new QaError(`用例校验失败：${errors.join('；')}`, EXIT.VALIDATION);
}

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: "cases", comment: false, script: "validate-cases" }),
);
