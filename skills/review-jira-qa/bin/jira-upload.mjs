#!/usr/bin/env node
/**
 * 上传阶段交付物并清理同交付物族的旧版本。
 * 规则：先全部上传成功，再删除旧版本；不删产品材料；同名同大小可跳过。
 */

import { open, readFile, stat, unlink } from 'node:fs/promises';
import path from 'node:path';

import { EXIT, QaError, boolArg, checkNode, loadCredentials, log, parseArgs, parseIssueKey, resolveWorkspace } from './lib/env.mjs';
import { JiraClient, attachmentList } from './lib/jira.mjs';
import { ensureDir, readText, writeJson } from './lib/fs-safe.mjs';
import { syncComment } from './lib/comment-sync.mjs';
import { handleFatal } from './lib/fatal.mjs';

function familyOf(filename) {
  const base = path.basename(filename);
  const m = base.match(/^(.*)-v(\d+)\.(\d+)\.(html|md|xmind)$/i);
  if (!m) return null;
  return { prefix: m[1], version: `${m[2]}.${m[3]}`, ext: `.${m[4].toLowerCase()}`, base };
}

const LOCK_STALE_MS = 30 * 60 * 1000;
const LOCK_POLL_MS = 2000;

function lockWaitMs() {
  const value = Number(process.env.QA_LOCK_WAIT_MS);
  return Number.isFinite(value) && value >= 0 ? value : 180000;
}

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

async function acquireLock(workspace, key) {
  const dir = path.join(workspace, '.qa', 'locks');
  await ensureDir(dir);
  const file = path.join(dir, `${key}.lock`);
  const waitMs = lockWaitMs();
  const deadline = Date.now() + waitMs;
  let notified = false;
  let staleAttempts = 0;
  for (;;) {
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
      if (!info) continue;
      if (Date.now() - info.mtimeMs > LOCK_STALE_MS) {
        staleAttempts += 1;
        await unlink(file).catch(() => {});
        if (staleAttempts > 3) {
          throw new QaError(`锁文件无法清除：${file}`, EXIT.INTERNAL);
        }
        continue;
      }
      if (Date.now() >= deadline) {
        throw new QaError(
          `该单正在被另一次运行处理，已等待 ${Math.round(waitMs / 1000)} 秒仍未释放：${file}`,
          EXIT.INTERNAL,
        );
      }
      if (!notified) {
        process.stderr.write(`[jira-upload][INFO] 该单正在被另一次运行处理，等待锁释放：${file}\n`);
        notified = true;
      }
      await sleep(LOCK_POLL_MS);
    }
  }
}

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const key = parseIssueKey(args.key || args._[0]);
  const workspace = resolveWorkspace(args.workspace);
  const runId = args['run-id'] || 'manual';
  const commentFile = args['comment-file'] ? path.resolve(String(args['comment-file'])) : '';
  const files = [];
  const fileArgs = Array.isArray(args.file) ? args.file : args.file ? [args.file] : [];
  for (const item of fileArgs) files.push(...String(item).split(',').map((x) => x.trim()).filter(Boolean));
  for (const item of args._.slice(1)) files.push(item);
  if (files.length === 0) {
    process.stderr.write('[jira-upload][ERR] 缺少 --file <交付物路径>。\n');
    throw new QaError('参数或配置错误', EXIT.USAGE);
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
        throw new QaError('校验失败', EXIT.VALIDATION);
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

    const warnings = [];
    const keepOld = args['keep-old'] === true || args['keep-old'] === 'true';
    if (!dryRun && !keepOld) {
      const after = attachmentList(await client.getIssue(key, { comments: false }));
      const newIds = new Set(uploaded.map((u) => u.id).filter(Boolean));
      const toDelete = after.filter((att) => {
        if (newIds.has(att.id)) return false;
        const fam = familyOf(att.filename);
        if (!fam) return false;
        return uploaded.some((u) => u.prefix === fam.prefix && u.ext === fam.ext);
      });
      for (const att of toDelete) {
        try {
          await client.deleteAttachment(att.id);
          actions.push({ action: 'delete', filename: att.filename, id: att.id });
        } catch (error) {
          // 旧版本由其他账号上传时无权删除（如平台公共账号）；本地调试按版本号区分即可，不视为失败。
          const reason = /403/.test(String(error.message))
            ? '无权限删除该旧版本（可能由其他账号上传），已保留并继续'
            : `未删除：${error.message}`;
          actions.push({ action: 'keep', filename: att.filename, id: att.id, reason });
          warnings.push(`旧版本 ${att.filename}（${att.id}）${reason}`);
          process.stderr.write(`[jira-upload][WARN] 旧版本 ${att.filename}（${att.id}）${reason}\n`);
        }
      }
    }

    const result = {
      ok: true,
      key,
      runId,
      dryRun,
      files: uploaded.map((u) => ({ path: u.path, filename: u.base, id: u.id, version: u.version, skipped: Boolean(u.skipped) })),
      actions,
      warnings,
    };
    await writeJson(path.join(workspace, '.qa', 'runs', runId, 'upload.json'), result);
    if (commentFile && !dryRun) {
      const body = await readText(commentFile);
      result.comment = await syncComment({ client, key, body, runId, workspace });
      await writeJson(path.join(workspace, '.qa', 'runs', runId, 'upload.json'), result);
    }
    for (const action of actions) {
      log('jira-upload', `${action.action} ${action.filename}${action.id ? ` id=${action.id}` : ''}${action.reason ? ` (${action.reason})` : ''}`);
    }
    process.stdout.write(`UPLOADED=${uploaded.length}\nACTIONS=${actions.length}\nDRY_RUN=${dryRun}\n`);
  } finally {
    await release();
  }
}

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: "", script: "jira-upload" }),
);
