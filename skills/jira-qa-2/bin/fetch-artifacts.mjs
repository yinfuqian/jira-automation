#!/usr/bin/env node
/**
 * 从 Jira 取回本技能阶段二产出的测试方案与测试点 XMind。
 *
 * 背景：平台每次任务都可能是空工作区，`locate.mjs --stage cases` 只读取本地交付目录
 * 与 Jira 附件列表，不会下载附件内容。阶段三开始前必须先把人工复核过的方案与 XMind
 * 落到 `outputs/<scope>/<KEY>/test-plans|test-cases/`，否则本地没有可供转换的基线。
 *
 * 规则：
 * - 只认 `<KEY>-` 开头、按 `-测试方案-vX.Y.md` / `-测试用例-vX.Y.xmind` 命名的附件；
 * - 方案与 XMind 各取最新版本；
 * - 本地已存在同版本或更高版本时不覆盖，避免冲掉人工复核后修改的文件；
 * - 找不到附件不算失败，输出 MISSING，由后续 `locate.mjs` 给出具体阻塞原因。
 */

import { existsSync } from 'node:fs';
import { readFile, readdir } from 'node:fs/promises';
import path from 'node:path';

import { checkNode, loadCredentials, parseArgs, parseIssueKey, resolveWorkspace } from './lib/env.mjs';
import { ensureDir, safeFilename, writeJson } from './lib/fs-safe.mjs';
import { JiraClient, attachmentList } from './lib/jira.mjs';
import { handleFatal } from './lib/fatal.mjs';

/** 解析交付物文件名中的版本号，非交付物返回 null。 */
function versionOf(filename, kind) {
  const pattern = kind === 'plan' ? /-测试方案-v(\d+)\.(\d+)\.md$/i : /-测试用例-v(\d+)\.(\d+)\.xmind$/i;
  const match = String(filename || '').match(pattern);
  if (!match) return null;
  return { major: Number(match[1]), minor: Number(match[2]) };
}

/** 比较两个版本，返回正数表示 a 更新。 */
function compareVersion(a, b) {
  if (!a) return -1;
  if (!b) return 1;
  return a.major === b.major ? a.minor - b.minor : a.major - b.major;
}

/** 在本地目录中找同类交付物的最新版本及其路径。 */
async function localLatest(dir, key, kind) {
  if (!existsSync(dir)) return null;
  const entries = await readdir(dir, { withFileTypes: true });
  let latest = null;
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.startsWith(`${key}-`)) continue;
    const version = versionOf(entry.name, kind);
    if (version && (!latest || compareVersion(version, latest.version) > 0)) {
      latest = { version, file: path.join(dir, entry.name) };
    }
  }
  return latest;
}

/** 读取 materials.json 中的 scope 与需求名称；文件不存在或解析失败时返回空对象。 */
async function readMaterials(file) {
  if (!file || !existsSync(file)) return {};
  try {
    return JSON.parse(await readFile(file, 'utf8'));
  } catch {
    return {};
  }
}

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const key = parseIssueKey(args.key || args._[0]);
  const workspace = resolveWorkspace(args.workspace);
  const runId = String(args['run-id'] || 'manual');
  const materials = await readMaterials(args.materials);
  const scope = String(args.scope || materials.scope || '');

  const root = path.join(workspace, 'outputs', ...(scope ? scope.split('/') : []), key);
  const planDir = path.join(root, 'test-plans');
  const xmindDir = path.join(root, 'test-cases');

  const client = new JiraClient(loadCredentials());
  const issue = await client.getIssue(key, { comments: false });
  const attachments = attachmentList(issue).filter((item) => String(item.filename || '').startsWith(`${key}-`));

  const picked = {};
  for (const kind of ['plan', 'xmind']) {
    const candidates = attachments
      .map((item) => ({ item, version: versionOf(item.filename, kind) }))
      .filter((entry) => entry.version);
    candidates.sort((a, b) => compareVersion(b.version, a.version));
    picked[kind] = candidates[0] || null;
  }

  const downloaded = [];
  const result = {
    ok: true,
    key,
    scope,
    root,
    plan: '',
    xmind: '',
    planVersion: picked.plan ? `${picked.plan.version.major}.${picked.plan.version.minor}` : '',
    xmindVersion: picked.xmind ? `${picked.xmind.version.major}.${picked.xmind.version.minor}` : '',
    downloaded,
    missing: [],
  };

  for (const [kind, target] of [
    ['plan', planDir],
    ['xmind', xmindDir],
  ]) {
    const candidate = picked[kind];
    if (!candidate) {
      result.missing.push(kind);
      continue;
    }
    const local = await localLatest(target, key, kind);
    const filename = safeFilename(candidate.item.filename, `${key}-${kind}`);
    const dest = path.join(target, filename);
    // 本地已有同版本或更高版本时直接复用，避免同版本多文件让 locate 无法唯一确定。
    if (local && compareVersion(local.version, candidate.version) >= 0) {
      result[kind] = local.file;
      continue;
    }
    await ensureDir(target);
    await client.downloadAttachment(candidate.item.id, dest);
    result[kind] = dest;
    downloaded.push({ kind, filename, id: String(candidate.item.id), bytes: candidate.item.size });
  }

  const runDir = path.join(workspace, '.qa', 'runs', runId);
  await ensureDir(runDir);
  await writeJson(path.join(runDir, 'fetch.json'), result);

  process.stdout.write(
    `SCOPE=${scope}\n` +
      `ROOT=${root}\n` +
      `PLAN=${result.plan}\n` +
      `XMIND=${result.xmind}\n` +
      `PLAN_VERSION=${result.planVersion}\n` +
      `XMIND_VERSION=${result.xmindVersion}\n` +
      `DOWNLOADED=${downloaded.length}\n` +
      `MISSING=${result.missing.join(',')}\n`,
  );
}

main().catch((error) => handleFatal(error, { argv: process.argv.slice(2), stage: 'cases', comment: false, script: 'fetch-artifacts' }));
