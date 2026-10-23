#!/usr/bin/env node
/**
 * 下载需求侧附件到 inputs/prd/<scope>/<KEY>/，抽取文本并写 materials.json。
 * 主需求材料不唯一时停止（exit 7），要求 --primary。
 */

import path from 'node:path';
import { readFile, writeFile } from 'node:fs/promises';

import {
  EXIT,
  QaError,
  checkNode,
  loadCredentials,
  log,
  normalizeScope,
  parseArgs,
  parseIssueKey,
  resolveWorkspace,
  sanitizeSegment,
} from './lib/env.mjs';
import { JiraClient, attachmentList } from './lib/jira.mjs';
import { ensureDir, safeFilename, writeJson } from './lib/fs-safe.mjs';
import { extractText } from './lib/office.mjs';

const REQUIREMENT_EXT = new Set(['.md', '.markdown', '.html', '.htm', '.pdf', '.docx', '.xlsx', '.pptx', '.txt', '.csv', '.png', '.jpg', '.jpeg']);
const EXCLUDED_PATTERN = /(技术方案|开发方案|技术协议|开发设计)/i;

function roleOf(filename) {
  const name = String(filename);
  if (EXCLUDED_PATTERN.test(name)) return 'excluded';
  if (/PRD|需求文档/i.test(name) || /\.md$/i.test(name)) return 'prd';
  if (/Overview|评审概览/i.test(name)) return 'overview';
  if (/Prototype|原型/i.test(name)) return 'prototype';
  if (/图示|DIAGRAM/i.test(name)) return 'diagram';
  if (/设计稿|Design/i.test(name)) return 'design';
  if (/模板|template/i.test(name) && /\.xlsx$/i.test(name)) return 'template';
  return 'auxiliary';
}

function suggestName(summary) {
  const stripped = String(summary || '')
    .replace(/【[^】]*】/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return sanitizeSegment(stripped || summary || '需求', '需求');
}

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const key = parseIssueKey(args.key || args._[0]);
  const workspace = resolveWorkspace(args.workspace);
  const runId = args['run-id'];
  if (!runId) {
    process.stderr.write('[jira-download][ERR] 缺少 --run-id；请先运行 jira-read.mjs。\n');
    process.exit(EXIT.USAGE);
  }
  const client = new JiraClient(loadCredentials());
  const issue = await client.getIssue(key, { comments: false });
  const attachments = attachmentList(issue);
  if (attachments.length === 0) {
    process.stderr.write(`[jira-download][ERR] ${key} 没有附件，无法取得需求材料。\n`);
    process.exit(EXIT.PRECONDITION);
  }

  const componentScope = (issue?.fields?.components || []).map((c) => c.name).filter(Boolean)[0] || '';
  const scope = normalizeScope(args.scope || process.env.QA_SCOPE || componentScope);
  const requirementName = suggestName(issue?.fields?.summary);
  const inputsDir = path.join(workspace, 'inputs', 'prd', ...(scope ? scope.split('/') : []), key);
  const extractedDir = path.join(inputsDir, '_extracted');
  await ensureDir(extractedDir);

  const candidates = attachments.filter((a) => REQUIREMENT_EXT.has(path.extname(a.filename).toLowerCase()));
  if (candidates.length === 0) {
    process.stderr.write(`[jira-download][ERR] ${key} 没有可识别的需求侧附件（支持 md/html/pdf/docx/xlsx/pptx/图片）。\n`);
    process.exit(EXIT.PRECONDITION);
  }

  const materials = [];
  for (const att of candidates) {
    const role = roleOf(att.filename);
    const filename = safeFilename(att.filename);
    const dest = path.join(inputsDir, filename);
    const downloaded = await client.downloadAttachment(att.id, dest, {
      maxMb: Number(args['max-mb'] || process.env.QA_MAX_ATTACHMENT_MB || 50),
    });
    const buffer = await readFile(dest);
    let extracted = { kind: 'binary', text: '', needsRead: true };
    try {
      extracted = extractText(filename, buffer);
    } catch (error) {
      extracted = { kind: 'unreadable', text: '', needsRead: false, error: error.message };
    }
    let extractedPath = '';
    if (extracted.text && extracted.text.trim()) {
      extractedPath = path.join(extractedDir, `${filename}.txt`);
      await writeFile(extractedPath, extracted.text, 'utf8');
    }
    materials.push({
      id: att.id,
      filename: att.filename,
      localFile: dest,
      size: att.size,
      created: att.created,
      role,
      kind: extracted.kind,
      needsRead: extracted.needsRead,
      unreadable: extracted.kind === 'unreadable',
      textChars: extracted.text ? extracted.text.length : 0,
      extractedPath,
    });
  }

  const prd = materials.filter((m) => m.role === 'prd');
  let primary = null;
  if (args.primary) {
    primary = materials.find((m) => m.id === String(args.primary)) || null;
    if (!primary) {
      process.stderr.write(`[jira-download][ERR] --primary ${args.primary} 不在附件列表中。\n`);
      process.exit(EXIT.PRECONDITION);
    }
  } else if (prd.length === 1) {
    primary = prd[0];
  } else if (prd.length > 1) {
    const sorted = [...prd].sort((a, b) => String(b.created).localeCompare(String(a.created)));
    if (sorted[0].created === sorted[1].created) {
      process.stderr.write(
        `[jira-download][ERR] 主需求材料不唯一：${prd.map((m) => m.id).join('、')}；请用 --primary 指定。\n`,
      );
      process.exit(EXIT.PRECONDITION);
    }
    primary = sorted[0];
  } else {
    const markdown = materials.filter((m) => /\.md$/i.test(m.filename));
    if (markdown.length === 1) primary = markdown[0];
    else {
      process.stderr.write('[jira-download][ERR] 找不到唯一的主需求材料（PRD .md）；请用 --primary 指定。\n');
      process.exit(EXIT.PRECONDITION);
    }
  }

  const result = {
    ok: true,
    key,
    runId,
    scope,
    requirementName,
    inputsDir,
    primaryId: primary.id,
    materials,
    counts: {
      total: materials.length,
      requirementSide: materials.filter((m) => m.role !== 'excluded').length,
      excluded: materials.filter((m) => m.role === 'excluded').length,
    },
  };
  const runDir = path.join(workspace, '.qa', 'runs', runId);
  await writeJson(path.join(runDir, 'materials.json'), result);
  log('jira-download', `scope=${scope || '(empty)'} primary=${primary.id} ${primary.filename}`);
  log('jira-download', `materials=${materials.length} inputs=${inputsDir}`);
  process.stdout.write(
    `SCOPE=${scope}\nREQUIREMENT_NAME=${requirementName}\nPRIMARY_ID=${primary.id}\nMATERIALS=${materials.length}\nINPUTS_DIR=${inputsDir}\n`,
  );
}

main().catch((error) => {
  const code = error instanceof QaError ? error.code : EXIT.INTERNAL;
  process.stderr.write(`[jira-download][ERR] ${error.message}\n`);
  process.exit(code);
});
