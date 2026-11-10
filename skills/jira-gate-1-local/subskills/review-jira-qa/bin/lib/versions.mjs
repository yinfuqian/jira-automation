/**
 * 交付物版本与文件名族。
 */

import path from 'node:path';

import { EXIT, fail, parseVersion } from './env.mjs';

export const KINDS = {
  gate: '需求准入门禁清单',
  plan: '测试方案',
  xmind: '测试用例',
};

export function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

export function versionedName(key, name, kind, version) {
  return `${key}-${name}-${KINDS[kind]}-v${version}${kind === 'gate' ? '.html' : kind === 'plan' ? '.md' : '.xmind'}`;
}

export function familyRegex(key, name, kind) {
  const suffix = kind === 'gate' ? '\\.html$' : kind === 'plan' ? '\\.md$' : '\\.xmind$';
  return new RegExp(`^${escapeRegExp(key)}-${escapeRegExp(name)}-${escapeRegExp(KINDS[kind])}-v(\\d+)\\.(\\d+)${suffix}$`);
}

export function compareVersion(a, b) {
  const va = parseVersion(`${a.major}.${a.minor}`);
  const vb = parseVersion(`${b.major}.${b.minor}`);
  return va.major === vb.major ? va.minor - vb.minor : va.major - vb.major;
}

export function pickLatest(files) {
  const parsed = files
    .map((file) => {
      const m = path.basename(file).match(/-v(\d+)\.(\d+)(?:\.[a-z]+)?$/i);
      if (!m) return null;
      return { file, major: Number(m[1]), minor: Number(m[2]) };
    })
    .filter(Boolean)
    .sort((a, b) => (a.major === b.major ? b.minor - a.minor : b.major - a.major));
  if (parsed.length === 0) return null;
  if (parsed.length > 1 && parsed[0].major === parsed[1].major && parsed[0].minor === parsed[1].minor) {
    fail(`存在多个同版本交付物，无法唯一确定：${parsed[0].file}、${parsed[1].file}`, EXIT.PRECONDITION);
  }
  return parsed[0];
}

export function isVersionedKind(filename, key, name, kind) {
  return familyRegex(key, name, kind).test(path.basename(filename));
}

export function nextDeliverableVersion(files) {
  const latest = pickLatest(files);
  if (!latest) return '1.0';
  return `${latest.major}.${latest.minor + 1}`;
}
