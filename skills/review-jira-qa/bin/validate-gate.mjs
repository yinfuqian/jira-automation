#!/usr/bin/env node
/**
 * 阶段一 HTML 结构校验：UTF-8、无外链、结论唯一、编号连续、禁用表述、版本一致。
 */

import path from 'node:path';

import { EXIT, QaError, checkNode, parseArgs } from './lib/env.mjs';
import { readJson, readText } from './lib/fs-safe.mjs';
import { validateGateFile } from './lib/html.mjs';
import { extractText } from './lib/office.mjs';
import { readFile } from 'node:fs/promises';
import { handleFatal } from './lib/fatal.mjs';

/** 阶段一必须逐项给出判定的判据，缺任一项即视为分析未完成。 */
export const CRITERIA = ['B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'C1', 'C2', 'C3', 'C4', 'C5', 'C6'];
const VERDICTS = ['已明确', '缺失', '不可验证'];
const ORIGINS = ['本版材料变更引入', '上一版漏检', '待确认来源', '不适用'];

function norm(text) {
  return String(text ?? '')
    .replace(/[\s　]+/g, '')
    .replace(/["'“”‘’]/g, '');
}

function validateChecks(spec, errors) {
  const checks = Array.isArray(spec.checks) ? spec.checks : [];
  const ids = new Set(checks.map((item) => String(item?.id || '')));
  const missing = CRITERIA.filter((id) => !ids.has(id));
  if (missing.length) errors.push(`checks 缺少判据：${missing.join('、')}`);
  for (const item of checks) {
    if (!VERDICTS.includes(String(item?.verdict))) {
      errors.push(`checks ${item?.id} 判定值非法：${item?.verdict}`);
    }
    if (!String(item?.basis || '').trim()) {
      errors.push(`checks ${item?.id} 缺少 basis（原文或章节依据）`);
    }
  }

  // 判据判为缺失/不可验证时，必须至少被一条不通过项引用，避免「判了缺失但清单不落项」。
  const coveredCriteria = new Set();
  for (const failure of spec.failures || []) {
    const label = failure?.topic || failure?.ac || '未命名项';
    const criteria = Array.isArray(failure?.criteria) ? failure.criteria.map(String) : [];
    if (!criteria.length) {
      errors.push(`不通过项「${label}」缺少 criteria（需声明对应判据 B1–B7 / C1–C6）`);
      continue;
    }
    for (const criterion of criteria) {
      if (!CRITERIA.includes(criterion)) {
        errors.push(`不通过项「${label}」criteria 非法：${criterion}`);
        continue;
      }
      coveredCriteria.add(criterion);
    }
  }
  for (const item of checks) {
    const verdict = String(item?.verdict || '');
    if ((verdict === '缺失' || verdict === '不可验证') && !coveredCriteria.has(String(item.id))) {
      errors.push(`判据 ${item.id} 判为「${verdict}」，但没有不通过项引用该判据`);
    }
    if (verdict === '已明确' && coveredCriteria.has(String(item.id))) {
      errors.push(`判据 ${item.id} 判为「已明确」，但有不通过项引用该判据`);
    }
  }
}

function validateOrigins(spec, errors) {
  for (const failure of spec.failures || []) {
    const label = failure.topic || failure.ac || '未命名项';
    if (!ORIGINS.includes(String(failure.origin))) {
      errors.push(`不通过项「${label}」origin 非法：${failure.origin}`);
    }
    if (failure.origin === '本版材料变更引入') {
      const quote = String(failure.introducedBy?.quote || '').trim();
      if (!quote) {
        errors.push(`不通过项「${label}」标注「本版材料变更引入」但未提供 introducedBy.quote`);
      }
    }
  }
}

function validateScans(spec, candidates, errors) {
  const list = Array.isArray(candidates?.candidates) ? candidates.candidates : [];
  if (!list.length) return;
  const answered = new Map(
    (Array.isArray(spec.scans) ? spec.scans : []).map((item) => [`${item?.rule}::${item?.target}`, item]),
  );
  for (const candidate of list) {
    const key = `${candidate.rule}::${candidate.target}`;
    const answer = answered.get(key);
    if (!answer) {
      errors.push(`扫描候选未回应：${candidate.rule} / ${candidate.target}`);
      continue;
    }
    if (!['确认', '驳回'].includes(String(answer.decision))) {
      errors.push(`扫描候选回应 decision 非法：${key} = ${answer.decision}`);
    }
    if (!String(answer.reason || '').trim()) {
      errors.push(`扫描候选回应缺少 reason：${key}`);
    }
  }
}

async function loadSources(files) {  const texts = [];
  for (const file of files) {
    try {
      const buffer = await readFile(file);
      const text = /\.(html?|htm)$/i.test(file) ? extractText(file, buffer).text : buffer.toString('utf8');
      texts.push({ file, text: norm(text) });
    } catch {
      /* 读取失败不阻断，交由缺源校验统一报错 */
    }
  }
  return texts;
}

function validateQuotes(spec, sources, errors) {
  if (!sources.length) return;
  for (const failure of spec.failures || []) {
    const label = failure.topic || failure.ac || '未命名项';
    for (const fact of failure.facts || []) {
      const quote = norm(fact?.quote);
      if (!quote) {
        errors.push(`不通过项「${label}」存在空的事实原文`);
        continue;
      }
      if (!sources.some((source) => source.text.includes(quote))) {
        errors.push(`不通过项「${label}」的事实原文未在材料中找到：${String(fact.quote).slice(0, 40)}…`);
      }
    }
  }
}

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const htmlPath = args.html;
  const specPath = args.spec;
  if (!htmlPath || !specPath) {
    process.stderr.write('[validate-gate][ERR] 需要 --html <file> --spec <gate.spec.json>。\n');
    throw new QaError('参数或配置错误', EXIT.USAGE);
  }
  const spec = await readJson(specPath);
  const result = await validateGateFile(htmlPath, spec, {
    deliverableVersion: args.version,
    filename: path.basename(htmlPath),
  });

  if (args['coverage-required']) {
    const required = await readJson(args['coverage-required']);
    const coverage = Array.isArray(spec.coverage) ? spec.coverage : [];
    const covered = new Set(coverage.map((c) => String(c.id)));
    const missing = (required.required || []).filter((id) => !covered.has(String(id)));
    if (missing.length > 0) {
      result.errors.push(`coverage 缺少 AC：${missing.join('、')}`);
    }
    for (const item of coverage) {
      if (!item || !item.id || item.checked !== true) {
        result.errors.push(`coverage 条目缺少 id 或 checked=true：${JSON.stringify(item)}`);
      } else if (!['通过', '不通过', '不适用'].includes(String(item.result))) {
        result.errors.push(`coverage 条目 result 非法：${item.id} = ${item.result}`);
      }
    }
    result.ok = result.errors.length === 0;
  }

  // 判定一致性校验：判据全覆盖、来源标注可核、事实原文可在材料中检索到。
  validateChecks(spec, result.errors);
  validateOrigins(spec, result.errors);
  const sourceArgs = args.source ? (Array.isArray(args.source) ? args.source : [args.source]) : [];
  const sources = await loadSources(sourceArgs);
  if (sourceArgs.length && !sources.length) {
    result.errors.push('--source 指定的材料全部读取失败');
  }
  validateQuotes(spec, sources, result.errors);
  if (args.candidates) {
    const candidates = await readJson(args.candidates).catch(() => null);
    if (!candidates) {
      result.errors.push('--candidates 指定的扫描结果无法读取');
    } else {
      validateScans(spec, candidates, result.errors);
    }
  }
  result.ok = result.errors.length === 0;

  if (args.json) {
    process.stdout.write(`${JSON.stringify({ ok: result.ok, errors: result.errors })}\n`);
  } else {
    for (const error of result.errors) process.stderr.write(`[validate-gate][ERR] ${error}\n`);
    process.stdout.write(result.ok ? 'VALIDATION=PASSED\n' : 'VALIDATION=FAILED\n');
  }
  if (!result.ok) throw new QaError(`HTML 校验失败：${result.errors.join('；')}`, EXIT.VALIDATION);
}

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: "risk-analysis", script: "validate-gate" }),
);
