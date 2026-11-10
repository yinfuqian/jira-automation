#!/usr/bin/env node
/**
 * workdir-cleanup.mjs —— jira-gate-2 收尾时清理本次任务的工作区中间产物。
 *
 * 用法：
 *   node scripts/workdir-cleanup.mjs --key <KEY> [--workdir <工作目录>] [--dry-run]
 *
 * 背景：本技能在 <工作目录>/work/jira-gate-2/<KEY>/ 下落附件、压缩包解压产物与中间文件
 * （attachments.txt / comment.txt / gate.json / issue.md 等）。按 SKILL.md 的规定这些都不是交付物，
 * 交付完成后应删掉，否则技能工作区里会按任务堆积一份份需求附件副本，大方案解压后有几十 MB。
 *
 * 硬约束：
 *   1. 只删「<工作目录>/work/jira-gate-2/<KEY>/」这一个目录：解析后的父目录必须正好是 work/jira-gate-2，
 *      目录名必须与 --key 一致，否则拒绝执行（防止路径写错把整个工作区删掉）。
 *   2. 交付失败（异常中断）时不要调用本脚本，保留现场便于排查。
 *   3. 绝不使用 --force 之类的逃生阀：这里没有需要绕过的检查，删不了就说明路径不对。
 */

import { existsSync, readdirSync, rmSync, statSync } from "node:fs";
import { dirname, join, parse as parsePath, resolve } from "node:path";
import { fileURLToPath } from "node:url";


// 中间产物所在的工作区子目录，与 SKILL.md 里的相对路径保持一致。
const GATE_SUBDIR = join("work", "jira-gate-2");


/** 收尾清理失败时抛出的错误，带可读 reason 便于写进对话摘要。 */
class CleanupError extends Error {
  constructor(message, reason) {
    super(message);
    this.name = "CleanupError";
    this.reason = reason || "unknown";
  }
}


/** 递归统计目录里的文件数与总字节数，只用于回显清理成果。 */
function measure(dir) {
  let files = 0;
  let bytes = 0;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const target = join(dir, entry.name);
    if (entry.isDirectory()) {
      const inner = measure(target);
      files += inner.files;
      bytes += inner.bytes;
    } else if (entry.isFile()) {
      files += 1;
      bytes += statSync(target).size;
    }
  }
  return { files, bytes };
}


/** 按 KEY 定位并删除本次任务的工作区中间产物目录。 */
function cleanup(options) {
  const key = String(options.key || "").trim().toUpperCase();
  if (!key) throw new CleanupError("缺少 --key（要清理的任务单号）", "key-missing");

  const workdir = resolve(String(options.workdir || "."));
  const gateRoot = resolve(workdir, GATE_SUBDIR);
  if (gateRoot === parsePath(gateRoot).root) {
    throw new CleanupError(`拒绝清理可疑路径：${gateRoot}`, "unsafe-path");
  }

  const dir = resolve(gateRoot, key);
  // 只处理 work/jira-gate-2/<KEY> 这一层：父目录必须正好是 work/jira-gate-2。
  // KEY 里带 / 或 .. 时会被 resolve 规范化，这里同样能拦住。
  if (dirname(dir) !== gateRoot) {
    throw new CleanupError(`拒绝清理 ${dir}：目标不在 ${gateRoot} 下`, "unsafe-path");
  }

  const base = { ok: true, key, workdir, dir };
  if (!existsSync(dir)) {
    // 已经清过、或本次没落中间文件：收尾是幂等的，不算失败。
    return { ...base, removed: false, reason: "not-found" };
  }
  if (!statSync(dir).isDirectory()) {
    throw new CleanupError(`拒绝清理 ${dir}：目标不是目录`, "not-a-directory");
  }

  const size = measure(dir);
  if (options["dry-run"]) {
    return { ...base, removed: false, reason: "dry-run", removedFileCount: size.files, removedBytes: size.bytes };
  }

  rmSync(dir, { recursive: true, force: true, maxRetries: 3 });
  if (existsSync(dir)) {
    throw new CleanupError(`清理后目录仍然存在：${dir}`, "remove-failed");
  }
  return { ...base, removed: true, removedFileCount: size.files, removedBytes: size.bytes };
}


/** 解析命令行参数，支持 --key value 与布尔开关。 */
function parseArgs(argv) {
  const options = {};
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (!item.startsWith("--")) continue;
    const next = argv[index + 1];
    if (next === undefined || String(next).startsWith("--")) {
      options[item.slice(2)] = true;
    } else {
      options[item.slice(2)] = next;
      index += 1;
    }
  }
  return options;
}


function main() {
  const options = parseArgs(process.argv.slice(2));
  if (!options.key) {
    // 与 cleanup() 里的缺参口径保持一致，调用方只需判断一个 reason。
    throw new CleanupError(
      "用法：node scripts/workdir-cleanup.mjs --key <KEY> [--workdir <工作目录>] [--dry-run]",
      "key-missing",
    );
  }
  process.stdout.write(JSON.stringify(cleanup(options), null, 2) + "\n");
}


// 被其它脚本作为库导入时不执行命令行逻辑。
const invokedDirectly = process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url));
if (invokedDirectly) {
  try {
    main();
  } catch (error) {
    const reason = error && error.reason ? `（${error.reason}）` : "";
    process.stderr.write(`[工作区清理失败]${reason} ${(error && error.message) || error}\n`);
    process.exitCode = 1;
  }
}
