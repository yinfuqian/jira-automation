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


/** 本地 mock 偶发连接失败（Windows 上端口/进程调度），有界重试避免假红。 */
async function fetchRetry(url, options = {}, attempts = 4) {
  let last;
  for (let i = 0; i < attempts; i += 1) {
    try {
      return await fetch(url, options);
    } catch (error) {
      last = error;
      await new Promise((resolve) => setTimeout(resolve, 250 * (i + 1)));
    }
  }
  throw last;
}

function parseLine(stdout, prefix) {
  const line = String(stdout).split(/\r?\n/).find((l) => l.startsWith(prefix));
  return line ? line.slice(prefix.length) : '';
}

async function startMockProcess(extra = []) {
  const proc = spawn(process.execPath, [path.join(ROOT, 'bin', 'mock-jira.mjs'), '--port', '0', ...extra], {
    stdio: ['ignore', 'pipe', 'pipe'],
  });
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
  process.stdout.write('qa-gate selftest\n');
  check(Number(process.versions.node.split('.')[0]) >= 20, `Node >= 20 (${process.versions.node})`);
  for (const file of ['SKILL.md', 'README.md', 'install.sh', 'package.sh', 'VERSION', 'templates/gate-html.html']) {
    check(existsSync(path.join(ROOT, file)), `存在 ${file}`);
  }

  // ---- 材料分类口径 ----
  const classifyLib = await import('./lib/classify.mjs');
  const materialSetLib = await import('./lib/material-set.mjs');
  check(classifyLib.classifyRole('image-2026-10-20-11-17-57-824.png') === 'image', '截图附件判为 image');
  check(
    materialSetLib.effectiveIds({ materials: [{ id: '9001', role: 'image', status: 'active' }] }).length === 0,
    '截图不计入材料集合',
  );
  check(
    classifyLib.classifyRole('主叫号码批量增删改查_业务需求说明书.docx') === 'prd-doc',
    '业务需求说明书 docx 判为 prd-doc',
  );
  check(
    classifyLib.hasPrdName('主叫号码批量增删改查_业务需求说明书.docx'),
    '需求说明书按 PRD 命名识别',
  );
  check(
    classifyLib.classifyRole('license主动释放_功能描述与技术方案.md') === 'prd-md',
    '功能描述与技术方案按需求材料处理',
  );
  check(classifyLib.classifyRole('X_开发设计说明书.md') === 'excluded', '纯开发设计材料仍排除');

  const workspace = await mkdtemp(path.join(os.tmpdir(), 'qa-gate-selftest-'));
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

    // ---- 结构扫描：干净材料零噪声，坏材料能被抓到 ----
    const fspScan = await import('node:fs/promises');
    const scanDir = path.join(workspace, '.qa', 'scanfx');
    await fspScan.mkdir(scanDir, { recursive: true });
    const cleanMd = path.join(scanDir, 'clean.md');
    await fspScan.writeFile(cleanMd, '# 示例需求\n\n#### 4.1.1 AC-001 第一条\n- THEN：「保存成功」\n\n#### 4.1.2 AC-002 第二条\n- THEN：「更新成功」\n');
    const cleanScan = run('scan.mjs', ['--source', cleanMd, '--out', path.join(scanDir, 'clean.json'), '--workspace', workspace], env);
    check(cleanScan.stdout.includes('CANDIDATES=0'), 'scan 在规范材料上零噪声', cleanScan.stderr);

    const brokenMd = path.join(scanDir, 'broken.md');
    await fspScan.writeFile(brokenMd, '# 示例需求\n\n#### 4.1.1 AC-001 第一条\n- THEN：「保存成功」\n\n#### 4.1.3 AC-003 第三条\n- THEN：「更新成功」\n\n参考 AC-009 的规则。\n');
    const brokenScan = run('scan.mjs', ['--source', brokenMd, '--out', path.join(scanDir, 'broken.json'), '--workspace', workspace], env);
    check(brokenScan.stdout.includes('CANDIDATES=2'), 'scan 抓到悬空编号与编号缺口', brokenScan.stderr);

    res = run('jira-read.mjs', ['--key', 'QA-101', '--workspace', workspace], env);
    check(res.status === 0, 'jira-read', res.stderr);
    const runId = parseLine(res.stdout, 'RUN_ID=');
    check(Boolean(runId), `runId=${runId}`);

    res = run('jira-download.mjs', ['--key', 'QA-101', '--run-id', runId, '--workspace', workspace], env);
    check(res.status === 0, 'jira-download', res.stderr);
    check(res.stdout.includes('MATERIALS=2') && res.stdout.includes('PRIMARY_ID=1001'), '下载 2 个附件');

    // ---- prepare 一次编排（阶段一） ----
    const prep = run('prepare.mjs', ['--key', 'QA-101', '--stage', '1', '--workspace', workspace], env);
    check(prep.status === 0, 'prepare 阶段一执行', prep.stderr);
    let prepJson = null;
    try {
      const line = prep.stdout
        .trim()
        .split(/\r?\n/)
        .filter((item) => item.trim().startsWith('{'))
        .pop();
      prepJson = JSON.parse(line);
    } catch {
      prepJson = null;
    }
    check(Boolean(prepJson) && prepJson.status === 'READY', 'prepare 返回 READY', String(prep.stdout).slice(0, 200));
    check(Boolean(prepJson) && prepJson.requiredAcs > 0, 'prepare 带出 AC 数量', prepJson && String(prepJson.requiredAcs));
    check(Boolean(prepJson) && existsSync(prepJson.digest), 'prepare 生成材料摘要');
    check(Boolean(prepJson) && prepJson.primaryId === '1001', 'prepare 带出主材料');

    res = run('materials.mjs', ['--run-id', runId, '--workspace', workspace], env);
    check(res.status === 0, 'materials', res.stderr);
    const digest = path.join(workspace, '.qa', 'runs', runId, 'materials-digest.md');
    check(existsSync(digest), 'materials-digest.md');
    check((await readFile(digest, 'utf8')).includes('脚本内正文'), 'JS 驱动 HTML 抽取脚本内正文');

    const coverageRun = run('coverage.mjs', ['--materials', path.join(workspace, '.qa', 'runs', runId, 'materials.json'), '--workspace', workspace], env);
    check(coverageRun.status === 0 && coverageRun.stdout.includes('REQUIRED_ACS=2'), 'coverage 提取全部 AC', coverageRun.stderr);
    const coverageRequired = parseLine(coverageRun.stdout, 'OUT=');
    check(existsSync(coverageRequired), '生成 coverage-required.json');

    const spec = path.join(ROOT, 'test', 'fixtures', 'gate-fail.json');
    res = run('render-gate.mjs', ['--spec', spec, '--workspace', workspace], env);
    check(res.status === 0, 'render-gate', res.stderr);
    const html = parseLine(res.stdout, 'HTML=');
    const comment = parseLine(res.stdout, 'COMMENT=');
    check(existsSync(html), '生成 HTML');
    check(existsSync(comment), '生成评论');

    res = run('validate-gate.mjs', ['--html', html, '--spec', spec, '--coverage-required', coverageRequired], env);
    check(res.status === 0, 'validate-gate + coverage', res.stderr);

    // ---- 判据缺失 / 事实原文不可检索 必须被拦住（独立 mock，避免异常评论污染后续计数） ----
    const vMock = await startMockProcess();
    try {
      const vEnv = { ...env, JIRA_BASE_URL: vMock.url };
      const fspGate = await import('node:fs/promises');
      const baseSpec = JSON.parse(await fspGate.readFile(spec, 'utf8'));
      const noChecks = { ...baseSpec };
      delete noChecks.checks;
      const noChecksPath = path.join(workspace, '.qa', 'runs', runId, 'gate-no-checks.json');
      await fspGate.writeFile(noChecksPath, JSON.stringify(noChecks));
      const noChecksRes = run('validate-gate.mjs', ['--html', html, '--spec', noChecksPath], vEnv);
      check(noChecksRes.stdout.includes('"status":"ERROR"'), 'validate-gate 拦截缺少判据的 spec', noChecksRes.stderr);

      const badQuote = JSON.parse(JSON.stringify(baseSpec));
      badQuote.failures[0].facts[0].quote = '这句话在材料里不存在';
      const badQuotePath = path.join(workspace, '.qa', 'runs', runId, 'gate-bad-quote.json');
      await fspGate.writeFile(badQuotePath, JSON.stringify(badQuote));
      const badQuoteRes = run(
        'validate-gate.mjs',
        ['--html', html, '--spec', badQuotePath, '--source', path.join(ROOT, 'test', 'fixtures', 'gate-fail.json')],
        vEnv,
      );
      check(badQuoteRes.stdout.includes('"status":"ERROR"'), 'validate-gate 拦截材料中检索不到的事实原文', badQuoteRes.stderr);

      const noScanPath = path.join(workspace, '.qa', 'runs', runId, 'gate-no-scan.json');
      await fspGate.writeFile(noScanPath, JSON.stringify(baseSpec));
      const noScanRes = run(
        'validate-gate.mjs',
        ['--html', html, '--spec', noScanPath, '--candidates', path.join(scanDir, 'broken.json')],
        vEnv,
      );
      check(noScanRes.stdout.includes('"status":"ERROR"'), 'validate-gate 要求逐条回应扫描候选', noScanRes.stderr);

      const looseCheck = JSON.parse(JSON.stringify(baseSpec));
      looseCheck.checks = looseCheck.checks.map((item) =>
        item.id === 'B3' ? { ...item, verdict: '不可验证' } : item,
      );
      const loosePath = path.join(workspace, '.qa', 'runs', runId, 'gate-loose-check.json');
      await fspGate.writeFile(loosePath, JSON.stringify(looseCheck));
      const looseRes = run('validate-gate.mjs', ['--html', html, '--spec', loosePath], vEnv);
      check(looseRes.stdout.includes('"status":"ERROR"'), '判据判缺失但清单未落项时校验失败', looseRes.stderr);
    } finally {
      vMock.proc.kill();
    }

    res = run('jira-upload.mjs', ['--key', 'QA-101', '--file', html, '--run-id', runId, '--workspace', workspace], env);
    check(res.status === 0, 'jira-upload', res.stderr);

    res = run('jira-comment.mjs', ['--key', 'QA-101', '--body', comment, '--run-id', runId, '--workspace', workspace], env);
    check(res.status === 0, 'jira-comment', res.stderr);
    const commentId1 = parseLine(res.stdout, 'COMMENT_ID=');

    res = run('result.mjs', ['--stage', 'risk-analysis', '--key', 'QA-101', '--run-id', runId, '--spec', spec, '--workspace', workspace], env);
    check(res.status === 0, 'result', res.stderr);
    check(res.stdout.includes('QA_RESULT_JSON'), '输出 QA_RESULT_JSON');
    check(res.stdout.includes('"conclusion":"暂不通过"'), '暂不通过仍输出 result');

    let state = await (await fetchRetry(`${env.JIRA_BASE_URL}/mock/state`, { headers: { Authorization: `Bearer ${MOCK_PAT}` } })).json();
    check(state.attachments.some((a) => a.filename.endsWith('.html')), 'Jira 存在阶段一附件');
    check(state.comments.length === 2, 'Jira 评论数为 2');

    const reuseRead = run('jira-read.mjs', ['--key', 'QA-101', '--workspace', workspace], env);
    const reuseRunId = parseLine(reuseRead.stdout, 'RUN_ID=');
    run('jira-download.mjs', ['--key', 'QA-101', '--run-id', reuseRunId, '--workspace', workspace], env);
    const reuse = run(
      'reuse.mjs',
      ['--key', 'QA-101', '--materials', path.join(workspace, '.qa', 'runs', reuseRunId, 'materials.json'), '--run-id', reuseRunId, '--workspace', workspace],
      env,
    );
    check(reuse.status === 0 && reuse.stdout.includes('REUSE=true'), '同材料复用', reuse.stderr);
    check(reuse.stdout.includes('PREVIOUS_VERSION=1.0') && reuse.stdout.includes('CONCLUSION=暂不通过'), '复用上一版结论与版本');

    res = run('jira-upload.mjs', ['--key', 'QA-101', '--file', html, '--run-id', runId, '--workspace', workspace], env);
    check(res.status === 0 && res.stderr.includes('skip'), '重复上传同文件跳过');

    res = run('jira-comment.mjs', ['--key', 'QA-101', '--body', comment, '--run-id', runId, '--workspace', workspace], env);
    check(res.status === 0 && res.stdout.includes('ACTION=edit'), '重复评论编辑原评论');
    check(parseLine(res.stdout, 'COMMENT_ID=') === commentId1, '评论 ID 不变');

    // 同标题同 PRD 版本号、但材料附件 ID 集合变化 → 必须新发一条评论
    const fspC = await import('node:fs/promises');
    const cDir = path.join(workspace, '.qa', 'runs', runId);
    const cA = path.join(cDir, 'c-a.md');
    const cB = path.join(cDir, 'c-b.md');
    await fspC.writeFile(cA, 'h3. QA-需求准入门禁结果v1.5\n\n*结论：暂不通过：1 项未关闭*\n\n材料版本：PRD v0.9（Jira 附件 7001、7002）\n');
    await fspC.writeFile(cB, 'h3. QA-需求准入门禁结果v1.6\n\n*结论：暂不通过：1 项未关闭*\n\n材料版本：PRD v0.9（Jira 附件 7001、7003）\n');
    const cResA = run('jira-comment.mjs', ['--key', 'QA-101', '--body', cA, '--run-id', runId, '--workspace', workspace], env);
    const cResB = run('jira-comment.mjs', ['--key', 'QA-101', '--body', cB, '--run-id', runId, '--workspace', workspace], env);
    check(cResB.stdout.includes('ACTION=create'), '材料附件 ID 变化时新发评论');
    check(parseLine(cResA.stdout, 'COMMENT_ID=') !== parseLine(cResB.stdout, 'COMMENT_ID='), '同版本号不同附件得到两条评论');

    res = run('render-gate.mjs', ['--spec', spec, '--version', '1.1', '--workspace', workspace], env);
    check(res.status === 0, 'render v1.1', res.stderr);
    const html11 = parseLine(res.stdout, 'HTML=');
    res = run('jira-upload.mjs', ['--key', 'QA-101', '--file', html11, '--run-id', runId, '--workspace', workspace], env);
    check(res.status === 0 && res.stderr.includes('delete'), '上传 v1.1 后删除 v1.0', res.stderr);
    state = await (await fetchRetry(`${env.JIRA_BASE_URL}/mock/state`, { headers: { Authorization: `Bearer ${MOCK_PAT}` } })).json();
    const gates = state.attachments.filter((a) => a.filename.includes('需求准入门禁清单'));
    check(gates.length === 1 && gates[0].filename.includes('v1.1'), '只保留最新门禁附件');
    const verRead = run('jira-read.mjs', ['--key', 'QA-101', '--workspace', workspace], env);
    const verRunId = parseLine(verRead.stdout, 'RUN_ID=');
    run('jira-download.mjs', ['--key', 'QA-101', '--run-id', verRunId, '--workspace', workspace], env);
    const verMaterials = JSON.parse(await readFile(path.join(workspace, '.qa', 'runs', verRunId, 'materials.json'), 'utf8'));
    check(verMaterials.suggestedDeliverableVersion === '1.2', '按已有门禁附件建议下一版本 1.2', verMaterials.suggestedDeliverableVersion);
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

    // ---- 阶段二终态推导：generated / reused / not-satisfied ----
    const fsp = await import('node:fs/promises');
    const matsPath = path.join(workspace, '.qa', 'runs', runId, 'materials.json');
    const gateOk = { status: '通过', conclusion: '通过', openCount: 0, stale: false, source: 'jira-comment' };
    const mkLocate = async (name, design, gate = gateOk) => {
      const file = path.join(workspace, '.qa', 'runs', runId, name);
      await fsp.writeFile(file, JSON.stringify({ key: 'QA-101', scope: '示例模块', gate, design, versions: {} }), 'utf8');
      return file;
    };
    const specOf = async (label, locateFile, extra = []) => {
      const out = path.join(workspace, '.qa', 'runs', runId, `design-${label}.json`);
      const r = run('design-spec.mjs', ['--key', 'QA-101', '--locate', locateFile, '--materials', matsPath, '--run-id', runId, '--out', out, '--workspace', workspace, ...extra], env);
      check(r.status === 0, `design-spec ${label}`, r.stderr);
      return out;
    };

    const genLocate = await mkLocate('locate-generated.json', { reuse: false, blocked: false, version: '1.0', reason: '材料已变化，需要生成新版本' });
    const genSpec = await specOf('generated', genLocate);
    const genJson = JSON.parse(await fsp.readFile(genSpec, 'utf8'));
    check(genJson.status === 'generated', '终态：材料变化 → generated', genJson.status);
    let rc = run('render-design-comment.mjs', ['--spec', genSpec, '--run-id', runId, '--workspace', workspace], env);
    check(rc.stdout.includes('QA-测试方案结果v1.0'), 'generated 产出结果评论');

    const reuseLocate = await mkLocate('locate-reused.json', { reuse: true, version: '1.0', reason: '材料未变化且 v1.0 产物齐备，复用上一版，不再生成' });
    const reuseSpec = await specOf('reused', reuseLocate);
    const reuseJson = JSON.parse(await fsp.readFile(reuseSpec, 'utf8'));
    check(reuseJson.status === 'reused', '终态：材料未变 → reused', reuseJson.status);
    check(reuseJson.deliverableVersion === '1.0', '复用不改版本号', reuseJson.deliverableVersion);
    rc = run('render-design-comment.mjs', ['--spec', reuseSpec, '--run-id', runId, '--workspace', workspace], env);
    check(rc.stdout.includes('COMMENT=none'), 'reused 零写入（不产出评论）');
    const reuseResult = run('result.mjs', ['--spec', reuseSpec, '--key', 'QA-101', '--run-id', runId, '--workspace', workspace], env);
    check(reuseResult.stdout.includes('"status":"OK"') && reuseResult.stdout.includes('"reused":true'), 'reused 映射 OK + reused=true');

    const blockedGate = { status: '暂不通过', conclusion: '暂不通过', openCount: 2, stale: false, source: 'jira-comment' };
    const blockedLocate = await mkLocate('locate-blocked.json', { reuse: false, blocked: true, blockReason: '阶段一结论：暂不通过（2 项未关闭）', version: '1.1' }, blockedGate);
    const blockedSpec = await specOf('not-satisfied', blockedLocate);
    const blockedJson = JSON.parse(await fsp.readFile(blockedSpec, 'utf8'));
    check(blockedJson.status === 'not-satisfied', '终态：门禁未过 → not-satisfied', blockedJson.status);
    check(String(blockedJson.reason).includes('暂不通过'), 'not-satisfied 带门禁原因', blockedJson.reason);
    rc = run('render-design-comment.mjs', ['--spec', blockedSpec, '--run-id', runId, '--workspace', workspace], env);
    check(rc.stdout.includes('QA-测试方案不满足执行条件'), 'not-satisfied 产出说明评论');
    const blockedResult = run('result.mjs', ['--spec', blockedSpec, '--key', 'QA-101', '--run-id', runId, '--workspace', workspace], env);
    check(blockedResult.stdout.includes('"status":"SKIPPED"') && blockedResult.stdout.includes('"applicable":false'), 'not-satisfied 映射 SKIPPED + applicable=false');

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

    // ---- 只有 PRD、没有 HTML：正常评审 ----
    await fetchRetry(`${env.JIRA_BASE_URL}/rest/api/2/attachment/1002`, {
      method: 'DELETE',
      headers: { Authorization: `Bearer ${MOCK_PAT}` },
    });
    const prdRead = run('jira-read.mjs', ['--key', 'QA-101', '--workspace', workspace], env);
    const prdRunId = parseLine(prdRead.stdout, 'RUN_ID=');
    const prdOnly = run('jira-download.mjs', ['--key', 'QA-101', '--run-id', prdRunId, '--workspace', workspace], env);
    check(prdOnly.status === 0 && !prdOnly.stdout.includes('"status":"SKIPPED"'), '只有 PRD 无 HTML 时正常评审', prdOnly.stderr);
    check(prdOnly.stdout.includes('PRIMARY_ID=1001'), '只有 PRD 时以 PRD 为主材料');

    // ---- 无附件但有 Jira 描述：正常评审，不中断 ----
    await fetchRetry(`${env.JIRA_BASE_URL}/rest/api/2/attachment/1001`, {
      method: 'DELETE',
      headers: { Authorization: `Bearer ${MOCK_PAT}` },
    });
    const descRead = run('jira-read.mjs', ['--key', 'QA-101', '--workspace', workspace], env);
    const descRunId = parseLine(descRead.stdout, 'RUN_ID=');
    const desc = run('jira-download.mjs', ['--key', 'QA-101', '--run-id', descRunId, '--workspace', workspace], env);
    check(desc.status === 0 && !desc.stdout.includes('"status":"SKIPPED"'), '只有 Jira 描述时正常评审', desc.stderr);
    const descMaterials = JSON.parse(await readFile(path.join(workspace, '.qa', 'runs', descRunId, 'materials.json'), 'utf8'));
    check(descMaterials.primaryId === 'issue-description', 'Jira 描述作为主需求材料');
    check(existsSync(path.join(descMaterials.inputsDir, 'QA-101-需求描述.md')), '生成 Jira 描述材料文件');

    // ---- 无附件且无描述：SKIPPED + 「评审不适用」 ----
    const emptyMock = await startMockProcess(['--empty']);
    try {
      const emptyEnv = { ...env, JIRA_BASE_URL: emptyMock.url };
      const emptyRead = run('jira-read.mjs', ['--key', 'QA-101', '--workspace', workspace], emptyEnv);
      const emptyRunId = parseLine(emptyRead.stdout, 'RUN_ID=');
      const emptySkip = run('jira-download.mjs', ['--key', 'QA-101', '--run-id', emptyRunId, '--workspace', workspace], emptyEnv);
      check(emptySkip.status === 0, '不适用输入 exit 0', emptySkip.stderr);
      check(emptySkip.stdout.includes('"status":"SKIPPED"'), '不适用输入输出 SKIPPED');
      check(emptySkip.stdout.includes('"applicable":false'), '不适用输入 applicable=false');
      const emptyResult = JSON.parse(await readFile(path.join(workspace, '.qa', 'runs', emptyRunId, 'result.json'), 'utf8'));
      check(['NO_ATTACHMENTS', 'NO_REQUIREMENT_MATERIALS'].includes(emptyResult.errorCode), '不适用输入写入 errorCode', emptyResult.errorCode);
      let emptyState = await (await fetchRetry(`${emptyMock.url}/mock/state`, { headers: { Authorization: `Bearer ${MOCK_PAT}` } })).json();
      const skipComment = emptyState.comments.find((c) => String(c.body || '').includes('评审不适用'));
      check(Boolean(skipComment), '不适用输入写「评审不适用」评论');
      check(Boolean(emptyResult.commentId), '不适用输入 result 带 commentId');
      check(!String(skipComment?.body || '').includes('QA-需求准入门禁结果v'), '不适用评论不是门禁结论');
      check(emptyState.comments.length === 1, '不适用评论只写一条');
      const emptyRead2 = run('jira-read.mjs', ['--key', 'QA-101', '--workspace', workspace], emptyEnv);
      const emptyRunId2 = parseLine(emptyRead2.stdout, 'RUN_ID=');
      run('jira-download.mjs', ['--key', 'QA-101', '--run-id', emptyRunId2, '--workspace', workspace], emptyEnv);
      emptyState = await (await fetchRetry(`${emptyMock.url}/mock/state`, { headers: { Authorization: `Bearer ${MOCK_PAT}` } })).json();
      check(emptyState.comments.length === 1, '不适用评论重复运行不新增');
    } finally {
      emptyMock.proc.kill();
    }

    // ---- 有需求侧附件但无主材料：描述不兜底，判不符合评审输入 ----
    const shortMock = await startMockProcess(['--short-description']);
    try {
      const shortEnv = { ...env, JIRA_BASE_URL: shortMock.url };
      const shortRead = run('jira-read.mjs', ['--key', 'QA-101', '--workspace', workspace], shortEnv);
      const shortRunId = parseLine(shortRead.stdout, 'RUN_ID=');
      const shortSkip = run('jira-download.mjs', ['--key', 'QA-101', '--run-id', shortRunId, '--workspace', workspace], shortEnv);
      check(shortSkip.stdout.includes('SKIPPED=NO_PRIMARY_MATERIAL'), '仅有数据类附件时判不符合评审输入', shortSkip.stderr);
      const shortResult = JSON.parse(await readFile(path.join(workspace, '.qa', 'runs', shortRunId, 'result.json'), 'utf8'));
      check(shortResult.applicable === false && shortResult.status === 'SKIPPED', '不符合评审输入 applicable=false');
    } finally {
      shortMock.proc.kill();
    }

    // ---- 单内没有任何需求侧附件：Jira 描述兜底为主材料 ----
    const descMock = await startMockProcess(['--description-only']);
    try {
      const descEnv = { ...env, JIRA_BASE_URL: descMock.url };
      const descRead = run('jira-read.mjs', ['--key', 'QA-101', '--workspace', workspace], descEnv);
      const descRunId = parseLine(descRead.stdout, 'RUN_ID=');
      const descRun = run('jira-download.mjs', ['--key', 'QA-101', '--run-id', descRunId, '--workspace', workspace], descEnv);
      check(descRun.stdout.includes('PRIMARY_ID=issue-description'), '无附件时描述兜底为主材料', descRun.stderr);
    } finally {
      descMock.proc.kill();
    }

    // ---- 旧版本由其他账号上传、当前账号无权删除：记 warning，不判失败 ----
    const denyMock = await startMockProcess(['--deny-delete']);
    try {
      const denyEnv = { ...env, JIRA_BASE_URL: denyMock.url };
      const fspDeny = await import('node:fs/promises');
      const denyDir = path.join(workspace, '.qa', 'runs', 'deny');
      await fspDeny.mkdir(denyDir, { recursive: true });
      const v10 = path.join(denyDir, 'QA-101-示例-需求准入门禁清单-v1.0.html');
      const v11 = path.join(denyDir, 'QA-101-示例-需求准入门禁清单-v1.1.html');
      await fspDeny.writeFile(v10, '<html><body>v1.0</body></html>');
      await fspDeny.writeFile(v11, '<html><body>v1.1</body></html>');
      const up1 = run('jira-upload.mjs', ['--key', 'QA-101', '--file', v10, '--run-id', 'deny', '--workspace', workspace], denyEnv);
      check(up1.status === 0, '无权删除场景：v1.0 上传成功', up1.stderr);
      const up2 = run('jira-upload.mjs', ['--key', 'QA-101', '--file', v11, '--run-id', 'deny', '--workspace', workspace], denyEnv);
      check(
        up2.status === 0 && !up2.stdout.includes('"status":"ERROR"'),
        '无权删除旧版本不判失败',
        up2.stderr,
      );
      const uploadJson = JSON.parse(await fspDeny.readFile(path.join(denyDir, 'upload.json'), 'utf8'));
      check(Array.isArray(uploadJson.warnings) && uploadJson.warnings.length > 0, '旧版本未删除记 warning');
      check(
        uploadJson.actions.some((item) => item.action === 'keep'),
        '旧版本转为 keep 动作',
      );
      check(
        uploadJson.files.some((file) => String(file.id || '')),
        '新版本仍成功上传',
      );
    } finally {
      denyMock.proc.kill();
    }

    // ---- 异常不中断：阶段一评论「执行异常」，阶段二只本地留痕 ----
    const errUpload = run(
      'jira-upload.mjs',
      ['--key', 'QA-101', '--file', 'missing.html', '--run-id', 'err-stage1', '--stage', 'risk-analysis', '--workspace', workspace],
      env,
    );
    check(errUpload.status === 0 && errUpload.stdout.includes('"status":"ERROR"'), '阶段一异常 exit 0 + status=ERROR', errUpload.stderr);
    state = await (await fetchRetry(`${env.JIRA_BASE_URL}/mock/state`, { headers: { Authorization: `Bearer ${MOCK_PAT}` } })).json();
    check(state.comments.some((c) => String(c.body || '').startsWith('执行异常')), '阶段一异常写「执行异常」评论');
    const commentsAfterStage1Error = state.comments.length;
    const errPlan = run('validate-plan.mjs', ['--plan', 'missing-plan.md', '--key', 'QA-101', '--stage', 'design'], env);
    check(errPlan.status === 0 && errPlan.stdout.includes('"status":"ERROR"'), '阶段二异常 exit 0 + status=ERROR', errPlan.stderr);
    state = await (await fetchRetry(`${env.JIRA_BASE_URL}/mock/state`, { headers: { Authorization: `Bearer ${MOCK_PAT}` } })).json();
    check(state.comments.some((c) => String(c.body || '').includes('QA-测试方案执行异常')), '阶段二异常写「执行异常」评论');
    check(state.comments.length === commentsAfterStage1Error + 1, '阶段二异常新增一条评论');

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
