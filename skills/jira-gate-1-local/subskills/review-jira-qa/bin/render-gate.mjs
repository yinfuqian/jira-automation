#!/usr/bin/env node
/**
 * gate.spec.json → 自包含 HTML + Jira wiki 评论。
 * 模型不直接写 HTML；结构与编号由本脚本固定。
 */

import path from 'node:path';

import { EXIT, QaError, checkNode, log, parseArgs, resolveWorkspace, skillDir, timestamp } from './lib/env.mjs';
import { readJson, writeFileAtomic, ensureDir, safeFilename } from './lib/fs-safe.mjs';
import { renderGate, renderComment, normalizeSpec } from './lib/html.mjs';
import { versionedName } from './lib/versions.mjs';
import { handleFatal } from './lib/fatal.mjs';

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const workspace = resolveWorkspace(args.workspace);
  const specPath = args.spec || args._[0];
  if (!specPath) {
    process.stderr.write('[render-gate][ERR] 缺少 --spec <gate.spec.json>。\n');
    throw new QaError('参数或配置错误', EXIT.USAGE);
  }
  const spec = normalizeSpec(await readJson(specPath));
  const version = String(args.version || spec.deliverableVersion || '1.0').replace(/^v/, '');
  const analysisTime = args.now || spec.analysisTime || timestamp();
  const name = safeFilename(spec.requirementName || '需求', '需求');
  const filename = args.out
    ? path.basename(args.out)
    : versionedName(spec.jiraKey, name, 'gate', version);
  const out = args.out
    ? path.resolve(workspace, args.out)
    : path.join(workspace, 'outputs', ...(spec.scope ? String(spec.scope).split('/') : []), spec.jiraKey, 'risk-analysis', filename);
  await ensureDir(path.dirname(out));

  const templatePath = args.template || path.join(skillDir(), 'templates', 'gate-html.html');
  const html = await renderGate(spec, { templatePath, deliverableVersion: version, analysisTime });
  await writeFileAtomic(out, html);
  const specOut = out.replace(/\.html$/i, '.spec.json');
  await writeFileAtomic(specOut, `${JSON.stringify({ ...spec, deliverableVersion: version, analysisTime, htmlFile: path.basename(out) }, null, 2)}\n`);

  const commentPath = args['comment-out'] || path.join(path.dirname(out), `${spec.jiraKey}-comment-v${version}.md`);
  const comment = renderComment(
    { ...spec, deliverableVersion: version, analysisTime },
    { filename: path.basename(out) },
  );
  await ensureDir(path.dirname(commentPath));
  await writeFileAtomic(commentPath, comment);

  const result = {
    ok: true,
    htmlPath: out,
    specPath: specOut,
    commentPath,
    filename: path.basename(out),
    deliverableVersion: version,
    analysisTime,
    conclusion: spec.conclusion,
    failures: spec.failures.length,
  };
  log('render-gate', `html=${out}`);
  log('render-gate', `comment=${commentPath}`);
  process.stdout.write(
    `HTML=${out}\nCOMMENT=${commentPath}\nFILENAME=${path.basename(out)}\nVERSION=${version}\nCONCLUSION=${spec.conclusion}\nFAILURES=${spec.failures.length}\n`,
  );
}

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: "risk-analysis", script: "render-gate" }),
);
