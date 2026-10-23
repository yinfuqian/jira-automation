#!/usr/bin/env node
/**
 * 阶段一 HTML 结构校验：UTF-8、无外链、结论唯一、编号连续、禁用表述、版本一致。
 */

import path from 'node:path';

import { EXIT, QaError, checkNode, parseArgs } from './lib/env.mjs';
import { readJson } from './lib/fs-safe.mjs';
import { validateGateFile } from './lib/html.mjs';

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const htmlPath = args.html;
  const specPath = args.spec;
  if (!htmlPath || !specPath) {
    process.stderr.write('[validate-gate][ERR] 需要 --html <file> --spec <gate.spec.json>。\n');
    process.exit(EXIT.USAGE);
  }
  const spec = await readJson(specPath);
  const result = await validateGateFile(htmlPath, spec, {
    deliverableVersion: args.version,
    filename: path.basename(htmlPath),
  });
  if (args.json) {
    process.stdout.write(`${JSON.stringify({ ok: result.ok, errors: result.errors })}\n`);
  } else {
    for (const error of result.errors) process.stderr.write(`[validate-gate][ERR] ${error}\n`);
    process.stdout.write(result.ok ? 'VALIDATION=PASSED\n' : 'VALIDATION=FAILED\n');
  }
  process.exit(result.ok ? EXIT.OK : EXIT.VALIDATION);
}

main().catch((error) => {
  const code = error instanceof QaError ? error.code : EXIT.INTERNAL;
  process.stderr.write(`[validate-gate][ERR] ${error.message}\n`);
  process.exit(code);
});
