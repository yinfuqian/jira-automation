/**
 * 文件安全：工作区围栏、原子写入、JSON、文件名清洗。
 */

import { createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';

import { EXIT, fail, isInside } from './env.mjs';

export function ensureInside(root, target) {
  const abs = path.resolve(root, target);
  if (!isInside(abs, root)) fail(`路径越出工作区：${target}`, EXIT.VALIDATION);
  return abs;
}

export async function ensureDir(dir) {
  await mkdir(dir, { recursive: true });
  return dir;
}

export async function writeFileAtomic(file, data) {
  await ensureDir(path.dirname(file));
  const tmp = `${file}.tmp-${process.pid}-${Date.now()}`;
  await writeFile(tmp, data, 'utf8');
  await rename(tmp, file);
  return file;
}

export async function writeJson(file, obj) {
  return writeFileAtomic(file, `${JSON.stringify(obj, null, 2)}\n`);
}

export async function readJson(file) {
  const text = await readFile(file, 'utf8');
  try {
    return JSON.parse(text);
  } catch (error) {
    fail(`JSON 解析失败：${file}（${error.message}）`, EXIT.VALIDATION);
  }
}

export async function readText(file) {
  return readFile(file, 'utf8');
}

export function sha256(text) {
  return createHash('sha256').update(text).digest('hex');
}

export function safeFilename(name, fallback = 'file') {
  const base = path.basename(String(name ?? ''));
  const cleaned = base
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/[. ]+$/g, '');
  const reserved = /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$/i.test(cleaned);
  return (reserved || !cleaned ? fallback : cleaned).slice(0, 160);
}

export function uniquePath(dir, filename) {
  let candidate = path.join(dir, filename);
  if (!existsSync(candidate)) return candidate;
  const ext = path.extname(filename);
  const stem = filename.slice(0, filename.length - ext.length);
  for (let i = 2; i < 1000; i += 1) {
    candidate = path.join(dir, `${stem}-${i}${ext}`);
    if (!existsSync(candidate)) return candidate;
  }
  fail(`无法为 ${filename} 生成唯一文件名`, EXIT.VALIDATION);
}

export function relative(root, file) {
  return path.relative(root, file).split(path.sep).join('/');
}
