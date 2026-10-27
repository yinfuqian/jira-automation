#!/usr/bin/env node
/**
 * 阶段一 HTML 结构校验：UTF-8、无外链、结论唯一、编号连续、禁用表述、版本一致。
 */

import path from 'node:path';

import { EXIT, QaError, checkNode, parseArgs } from './lib/env.mjs';
import { readJson } from './lib/fs-safe.mjs';
import { validateGateFile } from './lib/html.mjs';
import { handleFatal } from './lib/fatal.mjs';

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
