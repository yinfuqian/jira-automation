/**
 * 门禁证据：本地阶段一 spec 优先，其次 Jira 最新门禁评论。
 * 阶段二只读取，不因门禁不通过而停止。
 */

import { existsSync, readdirSync } from 'node:fs';
import path from 'node:path';

import { readJson } from './fs-safe.mjs';
import { parseMaterialLine, sameIdSet, sortIds } from './material-set.mjs';

const GATE_TITLE_RE = /^h3\.\s*\*{0,2}\s*QA-需求准入门禁结果v(\d+\.\d+)/i;

function versionOf(file) {
  const match = path.basename(file).match(/-v(\d+)\.(\d+)\.html$/i);
  return match ? [Number(match[1]), Number(match[2])] : [0, 0];
}

function latestGateFile(files) {
  return [...files].sort((a, b) => {
    const va = versionOf(a);
    const vb = versionOf(b);
    return va[0] === vb[0] ? vb[1] - va[1] : vb[0] - va[0];
  })[0] || '';
}

export function findGateRoots(workspace, key, scope) {
  if (!workspace) return [];
  const outputs = path.join(workspace, 'outputs');
  if (!existsSync(outputs)) return [];
  if (scope) {
    const root = path.join(outputs, ...String(scope).split('/'), key);
    return existsSync(root) ? [root] : [];
  }
  const roots = [];
  const walk = (dir, depth) => {
    if (depth > 3) return;
    let entries;
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      const full = path.join(dir, entry.name);
      if (entry.name === key) {
        roots.push(full);
        continue;
      }
      walk(full, depth + 1);
    }
  };
  walk(outputs, 0);
  return [...new Set(roots)];
}

export async function findLocalGate(workspace, key, scope) {
  const roots = findGateRoots(workspace, key, scope);
  const candidates = [];
  for (const root of roots) {
    const dir = path.join(root, 'risk-analysis');
    if (!existsSync(dir)) continue;
    let files = [];
    try {
      files = readdirSync(dir)
        .filter((name) => name.endsWith('.html') && name.includes('-需求准入门禁清单-v'))
        .map((name) => path.join(dir, name));
    } catch {
      continue;
    }
    if (!files.length) continue;
    const latest = latestGateFile(files);
    const specPath = latest.replace(/\.html$/i, '.spec.json');
    if (!existsSync(specPath)) continue;
    let spec;
    try {
      spec = await readJson(specPath);
    } catch {
      continue;
    }
    candidates.push({ root, html: latest, specPath, spec });
  }
  if (!candidates.length) return null;
  candidates.sort((a, b) => {
    const va = versionOf(a.html);
    const vb = versionOf(b.html);
    return va[0] === vb[0] ? vb[1] - va[1] : vb[0] - vb[1];
  });
  return candidates[0];
}

export function parseGateComment(body) {
  const text = String(body || '');
  const titleMatch = GATE_TITLE_RE.exec(text.trimStart());
  if (!titleMatch) return null;
  const conclusionMatch = /结论：\s*\*{0,2}\s*(通过|暂不通过)(?:：\s*(\d+)\s*项未关闭)?/.exec(text);
  const materialLine = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.includes('材料版本：'))
    .pop() || '';
  const { ids, hasDescription } = parseMaterialLine(materialLine);
  return {
    version: titleMatch[1],
    conclusion: conclusionMatch?.[1] || '',
    openCount: conclusionMatch?.[2] ? Number(conclusionMatch[2]) : 0,
    materialLine,
    materialIds: ids,
    hasDescription,
  };
}

export function latestGateComment(comments) {
  const parsed = (comments || [])
    .map((comment) => ({ comment, gate: parseGateComment(comment.body) }))
    .filter((item) => item.gate);
  if (!parsed.length) return null;
  return parsed[parsed.length - 1];
}

function gateFromSpec(spec) {
  const materials = Array.isArray(spec.materials) ? spec.materials : [];
  const materialIds = materials
    .filter((item) => item && item.id && item.id !== 'issue-description')
    .map((item) => String(item.id));
  return {
    version: String(spec.deliverableVersion || ''),
    conclusion: String(spec.conclusion || ''),
    openCount: Array.isArray(spec.failures) ? spec.failures.length : 0,
    materialLine: '',
    materialIds: sortIds(materialIds),
    hasDescription: materials.some((item) => item && item.id === 'issue-description'),
  };
}

export function statusText(gate) {
  if (!gate || !gate.conclusion) return '未找到';
  const base = gate.conclusion === '通过' ? '通过' : `暂不通过（${gate.openCount} 项未关闭）`;
  return gate.stale ? `${base}；材料已更新` : base;
}

export async function resolveGate({
  workspace,
  key,
  scope = '',
  currentIds = [],
  currentHasDescription = false,
  comments = [],
  allowJira = true,
} = {}) {
  const local = await findLocalGate(workspace, key, scope);
  let gate = null;
  let source = 'none';
  if (local?.spec) {
    gate = gateFromSpec(local.spec);
    source = 'local-spec';
  } else if (allowJira) {
    const latest = latestGateComment(comments);
    if (latest) {
      gate = { ...latest.gate, stale: false };
      source = 'jira-comment';
    }
  }
  if (!gate) {
    return { status: '未找到', conclusion: '', openCount: 0, stale: false, source, version: '', materialIds: [], hasDescription: false, materialMatch: null };
  }
  let stale = false;
  let materialMatch = null;
  if (gate.materialIds.length > 0 || gate.hasDescription) {
    materialMatch = sameIdSet(currentIds, gate.materialIds, {
      hasDescriptionA: currentHasDescription,
      hasDescriptionB: Boolean(gate.hasDescription),
    });
    stale = !materialMatch;
  }
  return {
    ...gate,
    status: stale ? '已过期' : gate.conclusion || '未找到',
    stale,
    source,
    materialMatch,
  };
}
