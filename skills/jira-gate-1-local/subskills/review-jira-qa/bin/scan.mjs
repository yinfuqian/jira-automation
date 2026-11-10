#!/usr/bin/env node
/**
 * 通用材料扫描：按 rules/scan-rules.json 的通用规则在需求材料上生成「候选问题」。
 *
 * 设计约束：
 * - 规则只依赖 Markdown 通用结构（标题、表格、编号、引号）与文档通用写法，不对任何具体 PRD 适配；
 * - 扫描只产候选，不下结论：每条候选必须由模型在 gate.spec.json 的 scans 里确认或驳回；
 * - 结果写入 candidates.json，供 validate-gate --candidates 做闭环校验。
 */

import path from 'node:path';
import { readFile } from 'node:fs/promises';

import { EXIT, QaError, checkNode, parseArgs, resolveWorkspace, skillDir } from './lib/env.mjs';
import { readJson, writeJson } from './lib/fs-safe.mjs';
import { extractText } from './lib/office.mjs';
import { handleFatal } from './lib/fatal.mjs';

function asArray(value) {
  if (Array.isArray(value)) return value.map(String);
  return value ? [String(value)] : [];
}

function normalizeId(id) {
  return String(id).replace(/－/g, '-');
}

async function loadSource(file) {
  const buffer = await readFile(file);
  const text = /\.(html?|htm)$/i.test(file) ? extractText(file, buffer).text : buffer.toString('utf8');
  return { file, lines: text.split(/\r?\n/), text };
}

function isTableLine(line) {
  return /^\s*\|.*\|\s*$/.test(line);
}

function splitCells(line) {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim());
}

function collectDefinitions(source, params) {
  const ids = new Set();
  const idRe = new RegExp(params.idPattern, 'g');
  for (const line of source.lines) {
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    if (heading) {
      for (const hit of heading[1].matchAll(idRe)) ids.add(normalizeId(hit[0]));
    }
    if (params.tableDefinition && isTableLine(line)) {
      const first = splitCells(line)[0] || '';
      for (const hit of first.matchAll(idRe)) ids.add(normalizeId(hit[0]));
    }
    if (params.lineDefinition && new RegExp(params.lineDefinition).test(line.trim())) {
      for (const hit of line.matchAll(idRe)) ids.add(normalizeId(hit[0]));
    }
  }
  return ids;
}

function ruleDanglingId(sources, rule) {
  const params = rule.params;
  const idRe = new RegExp(params.idPattern, 'g');
  const found = [];
  for (const source of sources) {
    const defined = collectDefinitions(source, params);
    source.lines.forEach((line, index) => {
      for (const hit of line.matchAll(idRe)) {
        const id = normalizeId(hit[0]);
        if (defined.has(id)) continue;
        found.push({
          rule: rule.id,
          target: id,
          file: source.file,
          line: index + 1,
          why: `${rule.why}（引用处：${line.trim().slice(0, 60)}）`,
        });
      }
    });
  }
  return found;
}

function ruleIdSequenceGap(sources, rule) {
  const params = rule.params;
  const defRe = new RegExp(params.sequencePattern);
  const groups = new Map();
  for (const source of sources) {
    source.lines.forEach((line, index) => {
      const hit = line.match(defRe);
      if (!hit) return;
      const kind = hit[1];
      const num = Number(hit[2]);
      if (!groups.has(kind)) groups.set(kind, []);
      groups.get(kind).push({ num, file: source.file, line: index + 1 });
    });
  }
  const found = [];
  for (const [kind, items] of groups) {
    if (items.length < params.minCount) continue;
    const numbers = new Set(items.map((item) => item.num));
    const max = Math.max(...numbers);
    const missing = [];
    for (let i = 1; i <= max; i += 1) if (!numbers.has(i)) missing.push(i);
    if (missing.length) {
      found.push({
        rule: rule.id,
        target: kind,
        file: items[0].file,
        line: items[0].line,
        why: `${rule.why}（${kind} 已定义 ${items.length} 项，最大编号 ${max}，缺号：${missing.join('、')}）`,
      });
    }
  }
  return found;
}

const RUNNERS = {
  'R1-DANGLING-ID': ruleDanglingId,
  'R2-ID-SEQUENCE-GAP': ruleIdSequenceGap,
};

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const workspace = resolveWorkspace(args.workspace);
  const files = asArray(args.source);
  if (!files.length) {
    throw new QaError('需要 --source <材料文件>，可重复传入。', EXIT.USAGE);
  }
  const rulesPath = args.rules ? path.resolve(args.rules) : path.join(skillDir(), 'rules', 'scan-rules.json');
  const ruleSet = await readJson(rulesPath);

  const sources = [];
  for (const file of files) {
    try {
      sources.push(await loadSource(file));
    } catch (error) {
      process.stderr.write(`[scan][WARN] 读取失败，跳过：${file}（${error.message}）\n`);
    }
  }
  if (!sources.length) throw new QaError('全部材料读取失败', EXIT.VALIDATION);

  const candidates = [];
  for (const rule of ruleSet.rules || []) {
    if (!rule.enabled) continue;
    const runner = RUNNERS[rule.id];
    if (!runner) {
      process.stderr.write(`[scan][WARN] 未实现的规则，跳过：${rule.id}\n`);
      continue;
    }
    candidates.push(...runner(sources, rule));
  }
  // 同一规则同一目标只保留一条，行号取首次命中
  const seen = new Set();
  const unique = candidates.filter((item) => {
    const key = `${item.rule}::${item.target}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });

  const out = args.out
    ? path.resolve(workspace, args.out)
    : path.join(workspace, '.qa', 'runs', args['run-id'] || 'manual', 'candidates.json');
  await writeJson(out, {
    version: ruleSet.version || 1,
    rules: rulesPath,
    sources: files,
    candidates: unique,
  });
  process.stdout.write(`CANDIDATES=${unique.length}\nOUT=${out}\n`);
  for (const item of unique) {
    process.stdout.write(`  ${item.rule} | ${item.target} | ${path.basename(item.file)}:${item.line}\n`);
  }
}

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: 'risk-analysis', comment: false, script: 'scan' }),
);
