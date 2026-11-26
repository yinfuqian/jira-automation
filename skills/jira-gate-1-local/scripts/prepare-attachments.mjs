#!/usr/bin/env node
/**
 * 准备本次 G1 预检的附件目录。
 *
 * 用法：
 *   node scripts/prepare-attachments.mjs --key <KEY> --workspace <工作目录> \
 *     [--local <本地附件文件或目录> ...]
 *
 * 规则：
 * - 提供 --local 时只使用本地文件，递归收集并复制到工作目录；本地输入无效时直接失败，不静默回退 Jira。
 * - 未提供 --local 时只读下载 Jira 单当前附件。
 * - 任何情况下都不向 Jira 写入。
 */

import { copyFileSync, existsSync, mkdirSync, readdirSync, statSync } from "node:fs";
import { basename, dirname, extname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { downloadAttachments, parseIssueKey } from "./jira.mjs";

/** 技能包根目录；附件目录不允许落进包里。 */
const SKILL_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

/** 判断 target 是否位于 root 之内（Windows 下忽略大小写）。 */
function isInside(target, root) {
  const normalize = (value) => (process.platform === "win32" ? value.toLowerCase() : value);
  const rel = normalize(relative(resolve(root), resolve(target)));
  return rel === "" || (!rel.startsWith("..") && !/^[A-Za-z]:/.test(rel));
}

/** 解析命令行参数；--local 可重复。 */
function parseArgs(argv) {
  const options = { local: [] };
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (!item.startsWith("--")) continue;
    const name = item.slice(2);
    const next = argv[index + 1];
    if (next === undefined || next.startsWith("--")) {
      options[name] = true;
      continue;
    }
    if (name === "local") options.local.push(next);
    else options[name] = next;
    index += 1;
  }
  return options;
}

/** 递归收集本地文件。 */
function collectLocalFiles(inputs) {
  const files = [];
  const walk = (target) => {
    const info = statSync(target);
    if (info.isFile()) {
      files.push(resolve(target));
      return;
    }
    if (!info.isDirectory()) return;
    for (const entry of readdirSync(target, { withFileTypes: true })) {
      walk(join(target, entry.name));
    }
  };
  for (const input of inputs) {
    const target = resolve(String(input));
    if (!existsSync(target)) throw new Error(`本地附件不存在：${target}`);
    walk(target);
  }
  return files;
}

/** 复制本地附件到目标目录，重名时追加序号。 */
function copyLocalFiles(files, dir) {
  mkdirSync(dir, { recursive: true });
  const used = new Set();
  const copied = [];
  for (const file of files) {
    const ext = extname(file);
    const stem = basename(file, ext);
    let name = basename(file);
    let index = 2;
    while (used.has(name.toLowerCase())) {
      name = `${stem}-${index}${ext}`;
      index += 1;
    }
    used.add(name.toLowerCase());
    const dest = join(dir, name);
    copyFileSync(file, dest);
    copied.push({ name, path: dest, bytes: statSync(dest).size, source: file });
  }
  return copied;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (!options.key || !options.workspace) {
    throw new Error("用法：--key <KEY> --workspace <工作目录> [--local <本地附件文件或目录> ...]");
  }
  const key = parseIssueKey(options.key);
  const workspace = resolve(String(options.workspace));
  // 防呆：工作区必须在技能包之外，避免需求附件被复制进技能安装目录。
  if (isInside(workspace, SKILL_ROOT)) {
    throw new Error(`工作区不能位于技能包内：${workspace}。请指向技能包外的工作目录。`);
  }
  const dir = join(workspace, "work", "jira-gate", key, "attachments");

  if (options.local.length > 0) {
    const files = collectLocalFiles(options.local);
    if (!files.length) throw new Error("本地附件目录为空，未找到任何文件。");
    const copied = copyLocalFiles(files, dir);
    process.stdout.write(`${JSON.stringify({ ok: true, key, source: "local", dir, files: copied }, null, 2)}\n`);
    return;
  }

  mkdirSync(dir, { recursive: true });
  const saved = await downloadAttachments(key, dir);
  const failed = saved.filter((item) => !item.path);
  const files = saved
    .filter((item) => item.path)
    .map((item) => ({
      name: item.filename,
      path: item.path,
      bytes: item.savedSize,
      source: "jira",
    }));
  // 没有附件时仍然成功返回，让调用方决定是否用 Jira 描述作为需求材料。
  const warning = files.length ? "" : `${key} 没有可下载的附件；如已有本地材料，请用 --local 指定。`;
  process.stdout.write(`${JSON.stringify({ ok: true, key, source: "jira", dir, files, failed, warning }, null, 2)}\n`);
}

main().catch((error) => {
  process.stderr.write(`[准备附件失败] ${error?.message || error}\n`);
  process.exitCode = 1;
});
