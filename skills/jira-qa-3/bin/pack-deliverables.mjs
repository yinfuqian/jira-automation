#!/usr/bin/env node
/**
 * 把 jira-qa 本轮产物打成 zip，放到工作区 `outputs/` 一层供接口下载。
 *
 * 平台交付物扫描只覆盖 `outputs/` 与 `reports/` 的一层，不下钻
 * `outputs/<scope>/<KEY>/test-cases/`。因此阶段二、阶段三结束时都要显式打包，
 * 调用方才能通过 `GET /skill/task/<task_id>/artifacts/<文件名>` 下载。
 */

import { existsSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';

import { checkNode, dateStamp, parseArgs, parseIssueKey, resolveWorkspace, EXIT, QaError } from './lib/env.mjs';
import { ensureDir, safeFilename } from './lib/fs-safe.mjs';
import { writeZip } from './lib/zip.mjs';
import { handleFatal } from './lib/fatal.mjs';

/** zip 内条目名重复时追加序号，避免相互覆盖。 */
function uniqueName(used, name) {
  if (!used.has(name)) {
    used.add(name);
    return name;
  }
  const ext = path.extname(name);
  const stem = name.slice(0, name.length - ext.length);
  for (let index = 2; index < 1000; index += 1) {
    const candidate = `${stem}-${index}${ext}`;
    if (!used.has(candidate)) {
      used.add(candidate);
      return candidate;
    }
  }
  throw new QaError(`无法为 ${name} 生成唯一压缩包条目名`, EXIT.VALIDATION);
}

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const key = parseIssueKey(args.key || args._[0]);
  const workspace = resolveWorkspace(args.workspace);
  const kind = String(args.kind || 'cases') === 'plan' ? 'plan' : 'cases';
  const files = [].concat(args.file || []);
  if (files.length === 0) throw new QaError('缺少 --file <交付物>，没有可打包的文件。', EXIT.USAGE);

  const used = new Set();
  const entries = {};
  for (const item of files) {
    const absolute = path.resolve(workspace, String(item));
    if (!existsSync(absolute) || !statSync(absolute).isFile()) {
      throw new QaError(`打包文件不存在：${absolute}`, EXIT.VALIDATION);
    }
    const name = uniqueName(used, safeFilename(path.basename(absolute), 'deliverable'));
    entries[name] = readFileSync(absolute);
  }

  const outDir = path.resolve(workspace, String(args['out-dir'] || path.join('outputs')));
  await ensureDir(outDir);
  const label = kind === 'plan' ? 'QA测试方案包' : 'QA交付包';
  const zipPath = path.join(outDir, String(args['zip-name'] || `${key}-${label}-${dateStamp()}.zip`));
  const buffer = writeZip(entries, { compress: true, date: new Date() });
  writeFileSync(zipPath, buffer);

  process.stdout.write(
    `ZIP=${zipPath}\n` +
      `ZIP_BYTES=${buffer.length}\n` +
      `ZIP_ENTRIES=${Object.keys(entries).length}\n` +
      `ENTRIES=${Object.keys(entries).join('|')}\n`,
  );
}

main().catch((error) => handleFatal(error, { argv: process.argv.slice(2), stage: 'cases', comment: false, script: 'pack-deliverables' }));
