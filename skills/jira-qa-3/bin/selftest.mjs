#!/usr/bin/env node
/**
 * 离线自检：安装包完整性 + 阶段边界契约 + 交付包 zip 可读。
 *
 * 以目录名判定当前技能：`jira-qa-2` 只允许阶段二，`jira-qa-3` 只允许阶段三。
 * 不访问 Jira，不执行任何阶段分析，也不依赖 Python。
 */

import { spawnSync } from 'node:child_process';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { packageStatus } from './preflight.mjs';
import { readZip } from './lib/zip.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SKILL_NAME = path.basename(ROOT);
const IS_STAGE2 = SKILL_NAME === 'jira-qa-2';
let failures = 0;

/** 记录一条断言结果，失败时给出可定位的明细。 */
function check(condition, message, detail = '') {
  if (condition) {
    process.stdout.write(`[OK] ${message}\n`);
    return;
  }
  failures += 1;
  process.stderr.write(`[FAIL] ${message}${detail ? `：${detail}` : ''}\n`);
}

async function main() {
  const pkg = packageStatus(ROOT);
  check(pkg.missing.length === 0, '安装包文件齐全', pkg.missing.join('、'));
  check(pkg.archiveOk, '压缩包解码器存在', pkg.archiveMissing.join('、'));
  check(pkg.version === '1.0.0', '版本号为 1.0.0', pkg.version);

  const skill = await readFile(path.join(ROOT, 'SKILL.md'), 'utf8');
  check(skill.includes(`name: ${SKILL_NAME}`), `front matter 名称为 ${SKILL_NAME}`);
  check(skill.includes(`/data/skills/${SKILL_NAME}`), '文档给出容器挂载路径');
  check(skill.includes('pack-deliverables.mjs'), '文档包含交付包打包步骤');
  check(skill.includes('.skill-result.json'), '文档包含机器可读结果契约');

  const prepare = await readFile(path.join(ROOT, 'bin', 'prepare.mjs'), 'utf8');
  check(prepare.includes(`${SKILL_NAME} 只执行阶段`), 'prepare.mjs 带阶段硬约束');

  const runtime = JSON.parse(await readFile(path.join(ROOT, 'runtime.json'), 'utf8'));
  const prompt = String(runtime.prompt_template || '');
  check(JSON.stringify(runtime.required_inputs) === JSON.stringify(['jira_url']), 'runtime 必填输入仅 jira_url');
  check(prompt.includes(SKILL_NAME), 'runtime 提示词引用本技能名');
  check(!prompt.includes('inputs.stage'), 'runtime 不使用 stage 入参');
  check(prompt.includes('pack-deliverables.mjs'), 'runtime 提示词包含打包步骤');

  if (IS_STAGE2) {
    check(skill.includes('只执行阶段二'), '文档声明只执行阶段二');
    check(skill.includes('不生成 Excel、Xray CSV 或测试代码'), '文档禁止生成阶段三产物');
    check(prompt.includes('禁止执行阶段一') && prompt.includes('阶段三'), 'runtime 禁止执行阶段一和阶段三');
    check(prompt.includes('--stage 2'), 'runtime 固定使用 --stage 2');
  } else {
    check(skill.includes('只执行阶段三'), '文档声明只执行阶段三');
    check(skill.includes('fetch-artifacts.mjs'), '文档包含 Jira 产物回取步骤');
    check(skill.includes('不写 Jira'), '文档声明不写 Jira');
    check(prompt.includes('fetch-artifacts.mjs'), 'runtime 提示词包含 Jira 产物回取');
    check(prompt.includes('--stage 3'), 'runtime 固定使用 --stage 3');
  }

  const workspace = await mkdtemp(path.join(os.tmpdir(), `${SKILL_NAME}-selftest-`));
  try {
    const first = path.join(workspace, `${SKILL_NAME === 'jira-qa-2' ? 'QA-101-示例-测试方案-v1.0.md' : 'QA-101-示例-Jira导入.csv'}`);
    const second = path.join(workspace, 'QA-101-示例-测试用例.xlsx');
    await writeFile(first, 'demo\n', 'utf8');
    await writeFile(second, 'demo\n', 'utf8');
    const res = spawnSync(
      process.execPath,
      [
        path.join(ROOT, 'bin', 'pack-deliverables.mjs'),
        '--key',
        'QA-101',
        '--kind',
        IS_STAGE2 ? 'plan' : 'cases',
        '--file',
        first,
        '--file',
        second,
        '--workspace',
        workspace,
      ],
      { encoding: 'utf8', env: process.env },
    );
    const zipPath = String(res.stdout || '')
      .split(/\r?\n/)
      .find((line) => line.startsWith('ZIP='))
      ?.slice(4);
    check(res.status === 0, 'pack-deliverables 退出码为 0', res.stderr);
    check(Boolean(zipPath) && existsSync(zipPath), '交付包已生成', zipPath);
    check(path.dirname(String(zipPath)) === path.join(workspace, 'outputs'), '交付包位于 outputs 顶层');
    check(path.basename(String(zipPath)).includes(IS_STAGE2 ? 'QA测试方案包' : 'QA交付包'), '交付包命名符合阶段');
    if (zipPath && existsSync(zipPath)) {
      const entries = readZip(await readFile(zipPath));
      check(entries.size === 2, 'zip 条目数为 2', String(entries.size));
      check(entries.has(path.basename(first)), 'zip 包含第一个交付物');
      check(entries.has(path.basename(second)), 'zip 包含第二个交付物');
    }
  } finally {
    await rm(workspace, { recursive: true, force: true });
  }

  if (failures > 0) {
    process.stderr.write(`SELFTEST=FAILED (${failures})\n`);
    process.exitCode = 1;
    return;
  }
  process.stdout.write('SELFTEST=PASSED\n');
}

main().catch((error) => {
  process.stderr.write(`[selftest][ERR] ${error.stack || error.message}\n`);
  process.exitCode = 1;
});
