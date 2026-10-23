#!/usr/bin/env node
/**
 * 离线自检：包结构、mock Jira 全链路、幂等、旧版本清理、密钥扫描。
 * 不访问真实 Jira。
 */

import { spawn, spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

import { skillDir } from './lib/env.mjs';
const MOCK_PAT = 'mock-pat-do-not-use-in-production';

const ROOT = skillDir();
let failures = 0;

function check(condition, label, detail = '') {
  if (condition) {
    process.stdout.write(`  OK   ${label}\n`);
  } else {
    failures += 1;
    process.stderr.write(`  FAIL ${label}${detail ? ` — ${detail}` : ''}\n`);
  }
}

function run(script, args, env) {
  const res = spawnSync(process.execPath, [path.join(ROOT, 'bin', script), ...args], {
    cwd: ROOT,
    env: { ...process.env, ...env },
    encoding: 'utf8',
  });
  return res;
}

function parseLine(stdout, prefix) {
  const line = String(stdout).split(/\r?\n/).find((l) => l.startsWith(prefix));
  return line ? line.slice(prefix.length) : '';
}

async function startMockProcess() {
  const proc = spawn(process.execPath, [path.join(ROOT, 'bin', 'mock-jira.mjs'), '--port', '0'], { stdio: ['ignore', 'pipe', 'pipe'] });
  const url = await new Promise((resolve, reject) => {
    let buf = '';
    const timer = setTimeout(() => reject(new Error('mock 启动超时')), 10000);
    proc.stdout.on('data', (d) => {
      buf += d;
      const m = buf.match(/MOCK_JIRA=(\S+)/);
      if (m) { clearTimeout(timer); resolve(m[1]); }
    });
    proc.on('exit', (code) => { clearTimeout(timer); reject(new Error(`mock 退出：${code}`)); });
  });
  return { proc, url };
}

async function main() {
  process.stdout.write('review-jira-qa selftest\n');
  check(Number(process.versions.node.split('.')[0]) >= 20, `Node >= 20 (${process.versions.node})`);
  for (const file of ['SKILL.md', 'README.md', 'install.sh', 'package.sh', 'VERSION', 'templates/gate-html.html']) {
    check(existsSync(path.join(ROOT, file)), `存在 ${file}`);
  }

  const workspace = await mkdtemp(path.join(os.tmpdir(), 'review-jira-qa-selftest-'));
  const { proc: mockProc, url: mockUrl } = await startMockProcess();
  const env = {
    JIRA_BASE_URL: mockUrl,
    JIRA_PAT: MOCK_PAT,
    QA_WORKSPACE: workspace,
    QA_TZ_OFFSET: '+08:00',
  };
  process.stdout.write(`mock=${env.JIRA_BASE_URL} workspace=${workspace}\n`);

  try {
    let res = run('preflight.mjs', ['--key', 'QA-101', '--workspace', workspace], env);
    check(res.status === 0, 'preflight', res.stderr);

    res = run('jira-read.mjs', ['--key', 'QA-101', '--workspace', workspace], env);
    check(res.status === 0, 'jira-read', res.stderr);
    const runId = parseLine(res.stdout, 'RUN_ID=');
    check(Boolean(runId), `runId=${runId}`);

    res = run('jira-download.mjs', ['--key', 'QA-101', '--run-id', runId, '--workspace', workspace], env);
    check(res.status === 0, 'jira-download', res.stderr);
    check(res.stdout.includes('MATERIALS=2'), '下载 2 个附件');

    res = run('materials.mjs', ['--run-id', runId, '--workspace', workspace], env);
    check(res.status === 0, 'materials', res.stderr);
    const digest = path.join(workspace, '.qa', 'runs', runId, 'materials-digest.md');
    check(existsSync(digest), 'materials-digest.md');
    check((await readFile(digest, 'utf8')).includes('脚本内正文'), 'JS 驱动 HTML 抽取脚本内正文');

    const spec = path.join(ROOT, 'test', 'fixtures', 'gate-fail.json');
    res = run('render-gate.mjs', ['--spec', spec, '--workspace', workspace], env);
    check(res.status === 0, 'render-gate', res.stderr);
    const html = parseLine(res.stdout, 'HTML=');
    const comment = parseLine(res.stdout, 'COMMENT=');
    check(existsSync(html), '生成 HTML');
    check(existsSync(comment), '生成评论');

    res = run('validate-gate.mjs', ['--html', html, '--spec', spec], env);
    check(res.status === 0, 'validate-gate', res.stderr);

    res = run('jira-upload.mjs', ['--key', 'QA-101', '--file', html, '--run-id', runId, '--workspace', workspace], env);
    check(res.status === 0, 'jira-upload', res.stderr);

    res = run('jira-comment.mjs', ['--key', 'QA-101', '--body', comment, '--run-id', runId, '--workspace', workspace], env);
    check(res.status === 0, 'jira-comment', res.stderr);
    const commentId1 = parseLine(res.stdout, 'COMMENT_ID=');

    res = run('result.mjs', ['--stage', 'risk-analysis', '--key', 'QA-101', '--run-id', runId, '--spec', spec, '--workspace', workspace], env);
    check(res.status === 0, 'result', res.stderr);
    check(res.stdout.includes('QA_RESULT_JSON'), '输出 QA_RESULT_JSON');
    check(res.stdout.includes('"conclusion":"暂不通过"'), '暂不通过仍输出 result');

    let state = await (await fetch(`${env.JIRA_BASE_URL}/mock/state`, { headers: { Authorization: `Bearer ${MOCK_PAT}` } })).json();
    check(state.attachments.some((a) => a.filename.endsWith('.html')), 'Jira 存在阶段一附件');
    check(state.comments.length === 2, 'Jira 评论数为 2');

    res = run('jira-upload.mjs', ['--key', 'QA-101', '--file', html, '--run-id', runId, '--workspace', workspace], env);
    check(res.status === 0 && res.stderr.includes('skip'), '重复上传同文件跳过');

    res = run('jira-comment.mjs', ['--key', 'QA-101', '--body', comment, '--run-id', runId, '--workspace', workspace], env);
    check(res.status === 0 && res.stdout.includes('ACTION=edit'), '重复评论编辑原评论');
    check(parseLine(res.stdout, 'COMMENT_ID=') === commentId1, '评论 ID 不变');

    res = run('render-gate.mjs', ['--spec', spec, '--version', '1.1', '--workspace', workspace], env);
    check(res.status === 0, 'render v1.1', res.stderr);
    const html11 = parseLine(res.stdout, 'HTML=');
    res = run('jira-upload.mjs', ['--key', 'QA-101', '--file', html11, '--run-id', runId, '--workspace', workspace], env);
    check(res.status === 0 && res.stderr.includes('delete'), '上传 v1.1 后删除 v1.0', res.stderr);
    state = await (await fetch(`${env.JIRA_BASE_URL}/mock/state`, { headers: { Authorization: `Bearer ${MOCK_PAT}` } })).json();
    const gates = state.attachments.filter((a) => a.filename.includes('需求准入门禁清单'));
    check(gates.length === 1 && gates[0].filename.includes('v1.1'), '只保留最新门禁附件');
    check(state.attachments.some((a) => a.filename.includes('PRD')), '未删除产品材料');

    // ---- 阶段二/三机械链路 ----
    const passSpec = path.join(ROOT, 'test', 'fixtures', 'gate-pass.json');
    res = run('render-gate.mjs', ['--spec', passSpec, '--workspace', workspace], env);
    check(res.status === 0, 'render 阶段一通过版', res.stderr);

    const xmindSpec = path.join(ROOT, 'test', 'fixtures', 'xmind-spec.json');
    res = run('write-xmind.mjs', ['--spec', xmindSpec, '--workspace', workspace], env);
    check(res.status === 0, 'write-xmind', res.stderr);
    const xmind = parseLine(res.stdout, 'XMIND=');
    check(existsSync(xmind), '生成 XMind');

    res = run('validate-xmind.mjs', ['--xmind', xmind], env);
    check(res.status === 0 && res.stdout.includes('"formalLeaves":2'), 'validate-xmind', res.stderr);

    res = run('locate.mjs', ['--stage', 'design', '--key', 'QA-101', '--workspace', workspace], env);
    check(res.status === 0 && res.stdout.includes('"conclusion":"通过"'), 'locate 阶段二门禁', res.stderr);

    const planSrc = path.join(ROOT, 'test', 'fixtures', 'reviewed-plan.md');
    const planDir = path.join(workspace, 'outputs', '示例模块', 'QA-101', 'test-plans');
    await (await import('node:fs/promises')).mkdir(planDir, { recursive: true });
    const plan = path.join(planDir, 'QA-101-支持列表筛选与导出-测试方案-v1.0.md');
    await (await import('node:fs/promises')).copyFile(planSrc, plan);
    res = run('validate-plan.mjs', ['--plan', plan, '--key', 'QA-101'], env);
    check(res.status === 0, 'validate-plan', res.stderr);
    res = run('mark-reviewed.mjs', ['--key', 'QA-101', '--plan', plan, '--xmind', xmind, '--reviewer', 'selftest', '--workspace', workspace], env);
    check(res.status === 0, 'mark-reviewed', res.stderr);
    res = run('locate.mjs', ['--stage', 'cases', '--key', 'QA-101', '--workspace', workspace], env);
    check(res.status === 0 && res.stdout.includes('"reviewed":true'), 'locate 阶段三门禁', res.stderr);

    res = run('extract-cases.mjs', ['--xmind', xmind, '--key', 'QA-101', '--name', '支持列表筛选与导出', '--workspace', workspace], env);
    check(res.status === 0 && res.stdout.includes('CASES=2'), 'extract-cases', res.stderr);
    const casesJson = parseLine(res.stdout, 'OUT=');
    const pyOk = spawnSync(process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3'), ['-c', 'import openpyxl'], { encoding: 'utf8' }).status === 0;
    if (pyOk) {
      res = run('build-cases.mjs', ['--cases', casesJson, '--key', 'QA-101', '--workspace', workspace], env);
      check(res.status === 0, 'build-cases', res.stderr);
      const xlsx = parseLine(res.stdout, 'XLSX=');
      const csv = parseLine(res.stdout, 'CSV=');
      res = run('validate-cases.mjs', ['--cases', casesJson, '--xlsx', xlsx, '--csv', csv, '--xmind', xmind, '--key', 'QA-101'], env);
      check(res.status === 0 && res.stdout.includes('"ok":true'), 'validate-cases', res.stderr);
    } else {
      process.stdout.write('  SKIP build-cases/validate-cases（未找到 Python + openpyxl）\n');
    }

    // 密钥/本机路径扫描（排除 mock 自身）
    const offenders = [];
    for (const dir of ['bin', 'prompts', 'templates']) {
      for (const file of await readdir(path.join(ROOT, dir), { recursive: true })) {
        const full = path.join(ROOT, dir, file);
        if (full.includes(`${path.sep}mock-jira.mjs`) || !/\.(mjs|md|html|json)$/.test(full)) continue;
        const text = await readFile(full, 'utf8');
        if (/JIRA_PAT\s*=\s*["'][^<]/.test(text) || /Authorization:\s*Bearer\s+[A-Za-z0-9]/.test(text)) offenders.push(full);
      }
    }
    check(offenders.length === 0, '无明文凭据', offenders.join(', '));
  } finally {
    mockProc.kill();
    if (process.env.QA_KEEP_SELFTEST !== '1') await rm(workspace, { recursive: true, force: true });
    else process.stdout.write(`保留自检工作区：${workspace}\n`);
  }

  if (failures > 0) {
    process.stderr.write(`SELFTEST=FAILED (${failures})\n`);
    process.exit(1);
  }
  process.stdout.write('SELFTEST=PASSED\n');
}

main().catch((error) => {
  process.stderr.write(`[selftest][ERR] ${error.stack || error.message}\n`);
  process.exit(1);
});
