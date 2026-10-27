#!/usr/bin/env node
/**
 * 下载需求侧附件到 inputs/prd/<scope>/<KEY>/，抽取文本并写 materials.json。
 * 输入不符合门禁要求（无附件/无需求侧材料/主材料不唯一）时写 SKIPPED 结果并以 exit 0 结束，不中断平台流程。
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
import { writeSkipResult } from './lib/result.mjs';
import { handleFatal } from './lib/fatal.mjs';

const REQUIREMENT_EXT = new Set(['.md', '.markdown', '.html', '.htm', '.pdf', '.docx', '.xlsx', '.pptx', '.txt', '.csv', '.png', '.jpg', '.jpeg']);
const EXCLUDED_PATTERN = /(技术方案|开发方案|技术协议|开发设计)/i;
const DELIVERABLE_PATTERN = /-(需求准入门禁清单|测试方案|测试用例)-v\d+\.\d+\.(html|md|xmind)$/i;

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

function suggestDeliverableVersion(attachments, key) {
  const versions = [];
  for (const att of attachments) {
    const name = String(att.filename || '');
    if (!name.startsWith(`${key}-`) || !name.endsWith('.html')) continue;
    if (!name.includes('-需求准入门禁清单-v')) continue;
    const m = name.match(/-v(\d+)\.(\d+)\.html$/);
    if (m) versions.push({ major: Number(m[1]), minor: Number(m[2]) });
  }
  if (versions.length === 0) return '1.0';
  versions.sort((a, b) => (a.major === b.major ? b.minor - a.minor : b.major - a.major));
  return `${versions[0].major}.${versions[0].minor + 1}`;
}

async function skipAndReturn(client, workspace, runId, key, { reason, errorCode, attachments }) {
  let commentId = '';
  const warnings = [];
  const body = `评审不适用\n原因：${reason}\n需求准入门禁未执行，未生成需求准入门禁清单。\n标记：${errorCode}`;
  try {
    const comments = await client.getComments(key);
    const existing = comments.find(
      (c) => String(c.body || '').includes('评审不适用') && String(c.body || '').includes('需求准入门禁未执行'),
    );
    if (existing) {
      await client.updateComment(key, existing.id, body);
      commentId = String(existing.id);
    } else {
      const created = await client.createComment(key, body);
      commentId = String(created.id);
    }
  } catch (error) {
    warnings.push(`评论写入失败：${error.message}`);
    process.stderr.write(`[jira-download][WARN] 评论写入失败：${error.message}\n`);
  }
  await writeSkipResult(workspace, runId, { jiraKey: key, reason, errorCode, attachments, commentId, warnings });
}

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const key = parseIssueKey(args.key || args._[0]);
  const workspace = resolveWorkspace(args.workspace);
  const runId = args['run-id'];
  if (!runId) {
    process.stderr.write('[jira-download][ERR] 缺少 --run-id；请先运行 jira-read.mjs。\n');
    throw new QaError('参数或配置错误', EXIT.USAGE);
  }
  const client = new JiraClient(loadCredentials());
  const issue = await client.getIssue(key, { comments: false });
  const attachments = attachmentList(issue);
  const rawDescription = issue?.fields?.description;
  const description =
    typeof rawDescription === 'string'
      ? rawDescription.trim()
      : rawDescription
        ? JSON.stringify(rawDescription, null, 2)
        : '';

  const componentScope = (issue?.fields?.components || []).map((c) => c.name).filter(Boolean)[0] || '';
  const scope = normalizeScope(args.scope || process.env.QA_SCOPE || componentScope);
  const requirementName = suggestName(issue?.fields?.summary);
  const inputsDir = path.join(workspace, 'inputs', 'prd', ...(scope ? scope.split('/') : []), key);
  const extractedDir = path.join(inputsDir, '_extracted');
  await ensureDir(extractedDir);

  const candidates = attachments.filter(
    (a) => !DELIVERABLE_PATTERN.test(a.filename) && REQUIREMENT_EXT.has(path.extname(a.filename).toLowerCase()),
  );
  if (attachments.length === 0 && !description) {
    await skipAndReturn(client, workspace, runId, key, {
      reason: `${key} 没有附件，也没有 Jira 需求描述，无法取得需求材料。`,
      errorCode: 'NO_ATTACHMENTS',
      attachments: [],
    });
    return;
  }
  if (candidates.length === 0 && !description) {
    await skipAndReturn(client, workspace, runId, key, {
      reason: `${key} 没有可识别的需求侧附件，也没有 Jira 需求描述。`,
      errorCode: 'NO_REQUIREMENT_MATERIALS',
      attachments,
    });
    return;
  }

  const materials = [];
  const deadline = Date.now() + Number(process.env.QA_DOWNLOAD_BUDGET_MS || 600000);
  for (const att of candidates) {
    if (Date.now() > deadline) {
      throw new QaError('附件下载总时长超过预算（QA_DOWNLOAD_BUDGET_MS）', EXIT.NETWORK);
    }
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

  for (const att of attachments) {
    if (candidates.some((c) => c.id === att.id)) continue;
    if (DELIVERABLE_PATTERN.test(att.filename)) continue;
    materials.push({
      id: att.id,
      filename: att.filename,
      localFile: '',
      size: att.size,
      created: att.created,
      role: 'unsupported',
      kind: 'unsupported',
      needsRead: false,
      unreadable: false,
      textChars: 0,
      extractedPath: '',
    });
  }

  const hasPrd = candidates.some((a) => roleOf(a.filename) === 'prd');
  const hasHtml = candidates.some((a) => /\.(html?|htm)$/i.test(a.filename));
  const needDescription = candidates.length === 0 || (!hasPrd && !hasHtml);
  let descriptionMaterial = null;
  if (description && needDescription) {
    const filename = `${key}-需求描述.md`;
    const content = `# ${issue?.fields?.summary || key}\n\n> 来源：Jira 需求字段（该单没有 PRD/HTML 附件时作为主需求材料）\n\n${description}\n`;
    const localFile = path.join(inputsDir, filename);
    await writeFile(localFile, content, 'utf8');
    const extractedPath = path.join(extractedDir, `${filename}.txt`);
    await writeFile(extractedPath, content, 'utf8');
    descriptionMaterial = {
      id: 'issue-description',
      filename,
      localFile,
      size: Buffer.byteLength(content),
      created: issue?.fields?.updated || '',
      role: 'prd',
      kind: 'text',
      needsRead: false,
      unreadable: false,
      textChars: content.length,
      extractedPath,
      source: 'jira-description',
    };
    materials.push(descriptionMaterial);
  }

  const prd = materials.filter((m) => m.role === 'prd' && m.id !== 'issue-description');
  let primary = null;
  if (args.primary) {
    primary = materials.find((m) => m.id === String(args.primary)) || null;
    if (!primary) {
      process.stderr.write(`[jira-download][ERR] --primary ${args.primary} 不在附件列表中。\n`);
      throw new QaError('前置条件不满足', EXIT.PRECONDITION);
    }
  } else if (prd.length === 1) {
    primary = prd[0];
  } else if (prd.length > 1) {
    const sorted = [...prd].sort((a, b) => String(b.created).localeCompare(String(a.created)));
    if (sorted[0].created === sorted[1].created) {
      await skipAndReturn(client, workspace, runId, key, {
        reason: `主需求材料不唯一：${prd.map((m) => m.id).join('、')}；需人工指定 --primary。`,
        errorCode: 'AMBIGUOUS_PRIMARY_MATERIAL',
        attachments: materials,
      });
      return;
    }
    primary = sorted[0];
  } else if (descriptionMaterial) {
    primary = descriptionMaterial;
  } else {
    const rank = { overview: 1, design: 2, prototype: 3, auxiliary: 4, template: 5 };
    const fallback = materials
      .filter((m) => !['excluded', 'unsupported', 'deliverable'].includes(m.role) && m.id !== 'issue-description')
      .sort((a, b) => (rank[a.role] || 9) - (rank[b.role] || 9) || (b.textChars || 0) - (a.textChars || 0));
    if (fallback.length > 0) primary = fallback[0];
    else {
      await skipAndReturn(client, workspace, runId, key, {
        reason: '找不到唯一的主需求材料（PRD .md），也没有 Jira 需求描述；需人工指定 --primary。',
        errorCode: 'NO_PRIMARY_MATERIAL',
        attachments: materials,
      });
      return;
    }
  }

  const result = {
    ok: true,
    key,
    runId,
    scope,
    requirementName,
    suggestedDeliverableVersion: suggestDeliverableVersion(attachments, key),
    inputsDir,
    primaryId: primary.id,
    materials,
    counts: {
      total: materials.length,
      requirementSide: materials.filter((m) => !['excluded', 'unsupported'].includes(m.role)).length,
      excluded: materials.filter((m) => m.role === 'excluded').length,
      unsupported: materials.filter((m) => m.role === 'unsupported').length,
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

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: "risk-analysis", script: "jira-download" }),
);
