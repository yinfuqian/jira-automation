#!/usr/bin/env node
/**
 * 验证 jira-gate-1-local 的关键安全与交付约束。
 * 本脚本只读包内文件并生成临时预检包，不访问网络或 Jira。
 */

import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';

const root = resolve(process.argv[2] || 'skills/jira-gate-1-local');
const failures = [];

/** 校验文件包含全部期望文本。 */
function expectContains(file, needles) {
  const text = readFileSync(join(root, file), 'utf8');
  for (const needle of needles) {
    if (!text.includes(needle)) failures.push(`${file} 缺少：${needle}`);
  }
}

expectContains('SKILL.md', [
  'name: jira-gate-1-local',
  '禁止写评论、回写字段、上传或删除附件、流转状态',
  'subskills/review-jira-req',
  'subskills/review-jira-design',
  'subskills/review-jira-qa',
  'pack-local-deliverables.mjs',
]);
expectContains('runtime.json', [
  'jira-gate-1-local',
  '全程禁止向 Jira 写入任何内容',
  '{skill_dir}/subskills/',
]);
expectContains('agents/openai.yaml', ['$jira-gate-1-local', 'subskills/']);
expectContains('scripts/jira-cli.mjs', ['DISABLED_COMMANDS', '本地预检技能禁止执行 Jira 写命令']);
expectContains('subskills/review-jira-qa/bin/lib/jira.mjs', [
  'isLocalGateMode',
  '本地预检技能禁止向 Jira 写入',
]);
expectContains('subskills/review-jira-qa/bin/lib/fatal.mjs', [
  '本地预检失败时不做“尽力写回 Jira”',
  '本地预检模式：异常信息只写入本地 result.json',
]);

for (const command of ['comment', 'gate-set', 'attach', 'transition']) {
  const result = spawnSync(process.execPath, [join(root, 'scripts/jira-cli.mjs'), command, '--body', 'blocked'], { encoding: 'utf8' });
  if (result.status === 0 || !result.stderr.includes('禁止执行 Jira 写命令')) {
    failures.push(`jira-cli 写命令未被禁用：${command}`);
  }
}

const temp = mkdtempSync(join(tmpdir(), 'jira-gate-local-'));
const commentPath = join(temp, 'comment.txt');
const reportPath = join(temp, 'report.md');
writeFileSync(commentPath, 'h3. G1 需求准入结论（本地预检）：通过\n# (/) *5.1 事实源*（MUST）\n');
writeFileSync(reportPath, '# 预检报告\n');
const outDir = join(temp, 'outputs');
const pack = spawnSync(process.execPath, [
  join(root, 'scripts/pack-local-deliverables.mjs'),
  '--key', 'TEST-1', '--comment', commentPath, '--report', reportPath, '--out', outDir,
], { encoding: 'utf8' });
if (pack.status !== 0) {
  failures.push(`打包脚本失败：${pack.stderr.trim()}`);
} else {
  const result = JSON.parse(pack.stdout);
  if (!result.ok || result.zipBytes <= 0) failures.push('打包结果不完整');
  if (result.entries.length !== 2) failures.push('打包条目数量不符合预期');
  if (!result.entries.some((entry) => entry.name === 'TEST-1-需求评审报告-宋立志.md')) failures.push('报告规范名未生效');
}
rmSync(temp, { recursive: true, force: true });

if (failures.length) {
  process.stderr.write(`[本地预检 skill 验证失败]\n- ${failures.join('\n- ')}\n`);
  process.exitCode = 1;
} else {
  process.stdout.write('jira-gate-1-local 验证通过\n');
}
