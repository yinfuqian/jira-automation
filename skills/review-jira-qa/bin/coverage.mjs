#!/usr/bin/env node
/**
 * 从主需求材料抽取全部 AC 编号，生成覆盖基线。
 * 模型必须按该列表填写 gate.spec.coverage；validate-gate 会校验是否有遗漏。
 */

import path from 'node:path';

import { EXIT, QaError, checkNode, log, parseArgs, resolveWorkspace } from './lib/env.mjs';
import { readJson, readText, writeJson } from './lib/fs-safe.mjs';

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const workspace = resolveWorkspace(args.workspace);
  const materialsPath = args.materials;
  if (!materialsPath) {
    process.stderr.write('[coverage][ERR] 缺少 --materials <materials.json>。\n');
    process.exit(EXIT.USAGE);
  }
  const materials = await readJson(materialsPath);
  const primary = (materials.materials || []).find((m) => String(m.id) === String(materials.primaryId));
  if (!primary) throw new QaError('materials.json 中找不到主需求材料。', EXIT.PRECONDITION);

  let text = '';
  if (primary.extractedPath) {
    text = await readText(primary.extractedPath);
  } else if (primary.localFile && /\.(md|markdown|txt|html?)$/i.test(primary.filename)) {
    text = await readText(primary.localFile);
  }
  const ids = [...new Set([...String(text).matchAll(/\bAC-\d{1,4}\b/g)].map((m) => m[0]))].sort();

  const out = args.out || path.join(workspace, '.qa', 'runs', materials.runId, 'coverage-required.json');
  const result = {
    source: primary.filename,
    primaryId: primary.id,
    required: ids,
    count: ids.length,
  };
  await writeJson(out, result);
  if (ids.length === 0) {
    process.stderr.write('[coverage][WARN] 主需求材料中未发现 AC 编号；如材料确实没有 AC，coverage 可为空。\n');
  }
  log('coverage', `required=${ids.length} out=${out}`);
  process.stdout.write(`REQUIRED_ACS=${ids.length}\nOUT=${out}\n${ids.join(',')}\n`);
}

main().catch((error) => {
  const code = error instanceof QaError ? error.code : EXIT.INTERNAL;
  process.stderr.write(`[coverage][ERR] ${error.message}\n`);
  process.exit(code);
});
