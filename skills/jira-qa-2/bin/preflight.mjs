#!/usr/bin/env node
/**
 * 前置检查：Node 版本、凭据、Issue Key、工作区、安装包完整性。
 * 不发起网络请求。
 *
 * 安装包完整性一次性列出全部缺失项：脚本缺失属于执行异常（不是「不满足执行条件」），
 * 必须在开始执行前暴露，不允许跑到中途才崩。
 */

import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

import { checkNode, loadCredentials, parseArgs, parseIssueKey, resolveWorkspace, log, skillDir, EXIT, QaError } from './lib/env.mjs';
import { handleFatal } from './lib/fatal.mjs';

/** 三个阶段共用的必需文件；缺任意一项即判定安装包不完整。 */
const REQUIRED_FILES = [
  'VERSION',
  'SKILL.md',
  'bin/preflight.mjs',
  'bin/jira-read.mjs',
  'bin/prepare.mjs',
  'bin/jira-download.mjs',
  'bin/reuse.mjs',
  'bin/materials.mjs',
  'bin/coverage.mjs',
  'bin/scan.mjs',
  'rules/scan-rules.json',
  'bin/render-gate.mjs',
  'bin/validate-gate.mjs',
  'bin/jira-upload.mjs',
  'bin/jira-comment.mjs',
  'bin/result.mjs',
  'bin/selftest.mjs',
  'bin/locate.mjs',
  'bin/design-spec.mjs',
  'bin/render-design-comment.mjs',
  'bin/validate-plan.mjs',
  'bin/write-xmind.mjs',
  'bin/validate-xmind.mjs',
  'bin/mark-reviewed.mjs',
  'bin/extract-cases.mjs',
  'bin/build-cases.mjs',
  'bin/validate-cases.mjs',
  'bin/fetch-artifacts.mjs',
  'bin/pack-deliverables.mjs',
  'bin/cases-result.mjs',
  'bin/lib/env.mjs',
  'bin/lib/fatal.mjs',
  'bin/lib/result.mjs',
  'bin/lib/classify.mjs',
  'bin/lib/material-set.mjs',
  'bin/lib/fs-safe.mjs',
  'command/qa-risk-analysis.md',
  'command/qa-design.md',
  'command/qa-cases.md',
  'command/jira-qa-2.md',
  'prompts/stage1-gate.md',
  'prompts/stage2-plan.md',
  'prompts/stage2-xmind.md',
  'prompts/stage3-cases.md',
];

const ARCHIVE_FILES = ['bin/vendor/libarchive-wasm/index.js', 'bin/vendor/libarchive-wasm/libarchive.wasm'];

function readVersion(root) {
  try {
    return readFileSync(path.join(root, 'VERSION'), 'utf8').trim();
  } catch {
    return '';
  }
}

export function packageStatus(root = skillDir()) {
  const missing = REQUIRED_FILES.filter((file) => !existsSync(path.join(root, file)));
  const missingArchive = ARCHIVE_FILES.filter((file) => !existsSync(path.join(root, file)));
  return {
    version: readVersion(root),
    missing,
    archiveOk: missingArchive.length === 0,
    archiveMissing: missingArchive,
    checked: REQUIRED_FILES.length,
  };
}

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const key = parseIssueKey(args.key || args._[0]);
  const workspace = resolveWorkspace(args.workspace);
  const scope = args.scope || process.env.QA_SCOPE || '';
  const creds = loadCredentials();
  const pkg = packageStatus();
  const out = {
    ok: pkg.missing.length === 0,
    version: pkg.version,
    node: process.versions.node,
    jiraKey: key,
    workspace,
    scope,
    jiraBaseUrl: creds.baseUrl,
    stage: args.stage || '1',
    archive: pkg.archiveOk ? 'ok' : 'missing',
    filesChecked: pkg.checked,
    missingFiles: pkg.missing,
  };
  if (!pkg.archiveOk) {
    out.warning = '压缩包解码器缺失（.rar/.7z 将记为不可读）';
  }
  if (args.json) process.stdout.write(`${JSON.stringify(out)}\n`);
  else {
    log('preflight', `version=${pkg.version || 'unknown'}`);
    log('preflight', `key=${key}`);
    log('preflight', `workspace=${workspace}`);
    log('preflight', `jira=${creds.baseUrl}`);
    if (!pkg.archiveOk) log('preflight', 'WARN 压缩包解码器缺失（.rar/.7z 将记为不可读）');
    if (pkg.missing.length) {
      log('preflight', `ERR 安装包不完整，缺少 ${pkg.missing.length} 项：`);
      for (const file of pkg.missing) log('preflight', `  - ${file}`);
    } else {
      log('preflight', `OK ${pkg.checked} 项文件齐全`);
    }
  }

  if (pkg.missing.length) {
    throw new QaError(
      `安装包不完整（版本 ${pkg.version || 'unknown'}）：缺少 ${pkg.missing.join('、')}`,
      EXIT.INTERNAL,
    );
  }
}

// 仅在作为脚本直接运行时执行检查；selftest 需要复用 packageStatus 而不触发校验。
const invokedDirectly = Boolean(process.argv[1]) && import.meta.url === pathToFileURL(process.argv[1]).href;
if (invokedDirectly) {
  main().catch((error) =>
    handleFatal(error, { argv: process.argv.slice(2), stage: "", script: "preflight" }),
  );
}
