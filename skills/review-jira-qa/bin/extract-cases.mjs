#!/usr/bin/env node
/**
 * XMind → cases.json（一叶一用例，正式叶子；【待确认】不计入）。
 */

import path from 'node:path';
import { readFile } from 'node:fs/promises';

import { EXIT, QaError, checkNode, parseArgs, parseIssueKey, resolveWorkspace } from './lib/env.mjs';
import { writeJson } from './lib/fs-safe.mjs';
import { parseXMind, walkLeaves } from './lib/xmind.mjs';

const PRIORITY = { 'priority-1': 'P0', 'priority-2': 'P1', 'priority-3': 'P2', 'priority-4': 'P3' };

function parseNotes(notes) {
  const sections = {};
  let current = '';
  for (const raw of String(notes || '').split(/\r?\n/)) {
    const line = raw.trim();
    const header = line.match(/^(用例属性|前置条件|测试步骤|预期结果|测试数据|补充说明)\s*$/);
    if (header) {
      current = header[1];
      sections[current] = [];
      continue;
    }
    if (current) sections[current].push(raw);
  }
  const text = (key) => (sections[key] || []).join('\n').trim();
  const attr = text('用例属性');
  const pick = (label) => {
    const m = attr.match(new RegExp(`${label}\\s*[：:]\\s*([^；;\\n]+)`));
    return m ? m[1].trim() : '';
  };
  return {
    type: pick('类型') || pick('主要类型'),
    priority: pick('优先级'),
    automation: pick('是否适合自动化'),
    precondition: text('前置条件'),
    steps: text('测试步骤'),
    expected: text('预期结果'),
    testData: text('测试数据'),
    remark: text('补充说明'),
  };
}

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const key = parseIssueKey(args.key || '');
  const workspace = resolveWorkspace(args.workspace);
  const xmind = args.xmind || args._[0];
  if (!xmind) {
    process.stderr.write('[extract-cases][ERR] 缺少 --xmind <file>。\n');
    process.exit(EXIT.USAGE);
  }
  const root = parseXMind(await readFile(xmind));
  const leaves = walkLeaves(root).filter((leaf) => !leaf.title.startsWith('【待确认】'));
  if (leaves.length === 0) throw new QaError('没有可转换的正式叶子。', EXIT.PRECONDITION);
  const cases = leaves.map((leaf, index) => {
    const notes = parseNotes(leaf.notes);
    const modulePath = [...leaf.ancestors].filter(Boolean).join(' / ');
    return {
      id: `${key}-TC${String(index + 1).padStart(3, '0')}`,
      module: modulePath || root.title,
      title: leaf.title,
      precondition: notes.precondition,
      testData: notes.testData,
      steps: notes.steps,
      expected: notes.expected,
      priority: notes.priority || PRIORITY[leaf.priority] || '',
      type: notes.type,
      automation: notes.automation,
      requirements: key,
      remark: notes.remark,
    };
  });
  const out = args.out
    ? path.resolve(workspace, args.out)
    : path.join(workspace, '.qa', `cases-${key}.json`);
  let scope = args.scope || '';
  if (!scope) {
    const normalized = path.resolve(xmind).split(path.sep).join('/');
    const m = normalized.match(/\/outputs\/(.+?)\/[^/]+\/test-cases\//);
    if (m) scope = m[1];
  }
  await writeJson(out, {
    jiraKey: key,
    requirementName: args.name || root.title,
    scope,
    sourceXmind: path.resolve(xmind),
    cases,
  });
  process.stdout.write(`CASES=${cases.length}\nOUT=${out}\n`);
}

main().catch((error) => {
  const code = error instanceof QaError ? error.code : EXIT.INTERNAL;
  process.stderr.write(`[extract-cases][ERR] ${error.message}\n`);
  process.exit(code);
});
