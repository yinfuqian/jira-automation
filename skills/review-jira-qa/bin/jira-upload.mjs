#!/usr/bin/env node
/**
 * 上传阶段交付物并清理同交付物族的旧版本。
 * 规则：先全部上传成功，再删除旧版本；不删产品材料；同名同大小可跳过。
 */

import { open, readFile, stat, unlink } from 'node:fs/promises';
import path from 'node:path';

import { EXIT, QaError, boolArg, checkNode, loadCredentials, log, parseArgs, parseIssueKey, resolveWorkspace } from './lib/env.mjs';
import { JiraClient, attachmentList } from './lib/jira.mjs';
import { ensureDir, writeJson } from './lib/fs-safe.mjs';

function familyOf(filename) {
  const base = path.basename(filename);
  const m = base.match(/^(.*)-v(\d+)\.(\d+)\.(html|md|xmind)$/i);
  if (!m) return null;
  return { prefix: m[1], version: `${m[2]}.${m[3]}`, ext: `.${m[4].toLowerCase()}`, base };
}

async function acquireLock(workspace, key) {
  const dir = path.join(workspace, '.qa', 'locks');
  await ensureDir(dir);
  const file = path.join(dir, `${key}.lock`);
  try {
    const handle = await open(file, 'wx');
    await handle.writeFile(`${process.pid} ${new Date().toISOString()}\n`);
    await handle.close();
    return async () => {
      try {
        await unlink(file);
      } catch {
        /* ignore */
      }
    };
  } catch (error) {
    if (error.code !== 'EEXIST') throw error;
    const info = await stat(file).catch(() => null);
    if (info && Date.now() - info.mtimeMs > 30 * 60 * 1000) {
      await unlink(file).catch(() => {});
      return acquireLock(workspace, key);
    }
    process.stderr.write(`[jira-upload][ERR] 该单正在被另一次运行处理：${file}\n`);
    process.exit(EXIT.PRECONDITION);
  }
}

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const key = parseIssueKey(args.key || args._[0]);
  const workspace = resolveWorkspace(args.workspace);
  const runId = args['run-id'] || 'manual';
  const files = [];
  const fileArgs = Array.isArray(args.file) ? args.file : args.file ? [args.file] : [];
  for (const item of fileArgs) files.push(...String(item).split(',').map((x) => x.trim()).filter(Boolean));
  for (const item of args._.slice(1)) files.push(item);
  if (files.length === 0) {
    process.stderr.write('[jira-upload][ERR] 缺少 --file <交付物路径>。\n');
    process.exit(EXIT.USAGE);
  }
  const dryRun = boolArg(args, 'dry-run');
  const forceUpload = boolArg(args, 'force-upload');
  const release = dryRun ? async () => {} : await acquireLock(workspace, key);

  try {
    const client = new JiraClient(loadCredentials(process.env, { required: !dryRun }));
    const issue = dryRun && !process.env.JIRA_BASE_URL ? null : await client.getIssue(key, { comments: false });
    const before = issue ? attachmentList(issue) : [];
    const uploaded = [];
    const actions = [];

    for (const file of files) {
      const abs = path.resolve(file);
      const family = familyOf(abs);
      if (!family) {
        process.stderr.write(`[jira-upload][ERR] 文件名不符合 <KEY>-<名称>-<类型>-v<版本>.<ext>：${abs}\n`);
        process.exit(EXIT.VALIDATION);
      }
      const local = await stat(abs);
      const same = before.find((a) => a.filename === path.basename(abs) && Number(a.size) === local.size);
      if (same && !forceUpload) {
        actions.push({ action: 'skip', filename: family.base, id: same.id, reason: '同名同大小已存在' });
        uploaded.push({ ...family, id: same.id, path: abs, skipped: true });
        continue;
      }
      if (dryRun) {
        actions.push({ action: 'upload', filename: family.base });
        uploaded.push({ ...family, id: null, path: abs });
        continue;
      }
      const att = await client.uploadAttachment(key, abs);
      actions.push({ action: 'upload', filename: family.base, id: String(att.id) });
      uploaded.push({ ...family, id: String(att.id), path: abs });
    }

    if (!dryRun) {
      const after = attachmentList(await client.getIssue(key, { comments: false }));
      const newIds = new Set(uploaded.map((u) => u.id).filter(Boolean));
      const toDelete = after.filter((att) => {
        if (newIds.has(att.id)) return false;
        const fam = familyOf(att.filename);
        if (!fam) return false;
        return uploaded.some((u) => u.prefix === fam.prefix && u.ext === fam.ext);
      });
      for (const att of toDelete) {
        await client.deleteAttachment(att.id);
        actions.push({ action: 'delete', filename: att.filename, id: att.id });
      }
    }

    const result = {
      ok: true,
      key,
      runId,
      dryRun,
      files: uploaded.map((u) => ({ path: u.path, filename: u.base, id: u.id, version: u.version, skipped: Boolean(u.skipped) })),
      actions,
    };
    await writeJson(path.join(workspace, '.qa', 'runs', runId, 'upload.json'), result);
    for (const action of actions) {
      log('jira-upload', `${action.action} ${action.filename}${action.id ? ` id=${action.id}` : ''}${action.reason ? ` (${action.reason})` : ''}`);
    }
    process.stdout.write(`UPLOADED=${uploaded.length}\nACTIONS=${actions.length}\nDRY_RUN=${dryRun}\n`);
  } finally {
    await release();
  }
}

main().catch((error) => {
  const code = error instanceof QaError ? error.code : EXIT.INTERNAL;
  process.stderr.write(`[jira-upload][ERR] ${error.message}\n`);
  process.exit(code === EXIT.OK ? EXIT.REMOTE_WRITE : code);
});
