#!/usr/bin/env node
/**
 * 验证 jira-gate-2-local 的关键安全与交付约束。
 * 本脚本只读取包内文件、拦截写入命令并生成临时预检包，不访问网络或 Jira。
 */

import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';

const root = resolve(process.argv[2] || 'skills/jira-gate-2-local');
const failures = [];

/** 校验文件包含全部期望文本。 */
function expectContains(file, needles) {
  const text = readFileSync(join(root, file), 'utf8');
  for (const needle of needles) {
    if (!text.includes(needle)) failures.push(`${file} 缺少：${needle}`);
  }
}

expectContains('SKILL.md', [
  'name: jira-gate-2-local',
  '全程禁止向 Jira 写入任何内容',
  '交付物只有一个本地预检包',
  '禁止调用 `comment` / `comment-update` / `attach` / `attach-prune` / `transition`',
  'pack-local-deliverables.mjs',
  '<KEY>-G2本地预检包-<时间戳>.zip',
]);
expectContains('runtime.json', [
  'jira-gate-2-local',
  '全程禁止向 Jira 写入任何内容',
  '禁止 jira-cli.mjs 的 comment / comment-update / gate-set / attach / attach-prune / transition',
  '<KEY>-G2本地预检包-<时间戳>.zip',
]);
expectContains('agents/openai.yaml', ['$jira-gate-2-local', '不上传、不评论、不回写、不流转']);
expectContains('scripts/jira-cli.mjs', ['DISABLED_COMMANDS', '本地预检技能禁止执行 Jira 写命令']);
expectContains('scripts/jira.mjs', [
  'isLocalGateMode',
  '本地预检技能禁止向 Jira 写入',
]);

for (const command of ['comment', 'comment-update', 'gate-set', 'attach', 'attach-prune', 'transition']) {
  const result = spawnSync(process.execPath, [
    join(root, 'scripts/jira-cli.mjs'),
    command,
    'TEST-1',
    '--body',
    'blocked',
  ], { encoding: 'utf8', env: { ...process.env, JIRA_GATE_LOCAL_MODE: '1' } });
  if (result.status === 0 || !result.stderr.includes('禁止执行 Jira 写命令')) {
    failures.push(`jira-cli 写命令未被禁用：${command}`);
  }
}

const temp = mkdtempSync(join(tmpdir(), 'jira-gate-2-local-'));
const commentPath = join(temp, 'comment.txt');
const pdcaPath = join(temp, 'pdca.md');
writeFileSync(commentPath, 'h3. G2 技术方案准出结论（本地预检）：通过\n* 结论：通过\n');
writeFileSync(pdcaPath, '# PDCA RD 报告\n');
const outDir = join(temp, 'outputs');
const pack = spawnSync(process.execPath, [
  join(root, 'scripts/pack-local-deliverables.mjs'),
  '--key', 'TEST-1',
  '--comment', commentPath,
  '--pdca', pdcaPath,
  '--out', outDir,
], { encoding: 'utf8' });
if (pack.status !== 0) {
  failures.push(`打包脚本失败：${pack.stderr.trim()}`);
} else {
  const result = JSON.parse(pack.stdout);
  if (!result.ok || result.zipBytes <= 0) failures.push('打包结果不完整');
  if (result.entries.length !== 2) failures.push('打包条目数量不符合预期');
  if (!result.entries.some((entry) => entry.name === 'TEST-1-PDCA-RD报告.md')) failures.push('PDCA 报告规范名未生效');
  if (!result.entries.some((entry) => entry.name === 'TEST-1-G2预检评论.txt')) failures.push('评论草稿规范名未生效');
}
rmSync(temp, { recursive: true, force: true });

if (failures.length) {
  process.stderr.write(`[本地预检 skill 验证失败]\n- ${failures.join('\n- ')}\n`);
  process.exitCode = 1;
} else {
  process.stdout.write('jira-gate-2-local 验证通过\n');
}
