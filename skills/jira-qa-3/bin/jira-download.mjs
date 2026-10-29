#!/usr/bin/env node
/**
 * 下载需求侧附件到 inputs/prd/<scope>/<KEY>/，抽取文本并写 materials.json。
 *
 * 规则：
 * - 只认需求材料：排除 QA 交付物、评审产物、开发设计材料；
 * - 同名附件保留 created 最新，其余 superseded；
 * - .rar/.7z/.zip 用包内 WASM 解包，只解白名单内的需求材料；
 * - 主材料优先级：需求 MD > docx/xlsx PRD > Jira 描述 > HTML/原型兜底；
 * - 输入不适用时写 SKIPPED 结果并以 exit 0 结束。
 */

import path from 'node:path';
import { readFile } from 'node:fs/promises';

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
import {
  ensureDir,
  ensureInside,
  safeFilename,
  writeBinaryAtomic,
  writeFileAtomic,
  writeJson,
} from './lib/fs-safe.mjs';
import { extractText } from './lib/office.mjs';
import { archiveLimits, inspectArchive } from './lib/archive.mjs';
import {
  basenameOf,
  classifyRole,
  extensionOf,
  hasPrdName,
  isArchiveExtension,
  isExcludedRole,
  isRequirementExtension,
} from './lib/classify.mjs';
import { IGNORED_ROLES } from './lib/material-set.mjs';
import { writeSkipResult } from './lib/result.mjs';
import { syncStatusComment } from './lib/comment-sync.mjs';
import { handleFatal } from './lib/fatal.mjs';

const ACTIVE_ROLE_EXCLUDES = new Set([
  'archive',
  'qa-deliverable',
  'qa-generated',
  'excluded',
  'superseded',
  'unsupported',
]);

/** 兜底主材料只接受文档类扩展名，排除扫描结果表、截图等数据文件。 */
const REQUIREMENT_DOC_EXTENSIONS = new Set(['.md', '.markdown', '.txt', '.html', '.htm', '.pdf', '.docx', '.doc']);

function stageFromArgv(argv) {
  const args = parseArgs(argv);
  const raw = String(args.stage || '');
  if (raw === '1') return 'risk-analysis';
  if (raw === '2') return 'design';
  if (raw === '3') return 'cases';
  return raw || 'risk-analysis';
}

function suggestName(summary) {
  const stripped = String(summary || '')
    .replace(/【[^】]*】/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return sanitizeSegment(stripped || summary || '需求', '需求');
}

function versionFromName(name) {
  const match = /(?:^|[_\-\s])v?(\d+)\.(\d+)(?:[_\-\s.]|$)/i.exec(String(name || ''));
  return match ? `${Number(match[1])}.${Number(match[2])}` : '';
}

function suggestDeliverableVersion(attachments, key) {
  const versions = [];
  for (const att of attachments) {
    const name = String(att.filename || '');
    if (!name.startsWith(`${key}-`) || !name.endsWith('.html')) continue;
    if (!name.includes('-需求准入门禁清单-v')) continue;
    const match = name.match(/-v(\d+)\.(\d+)\.html$/);
    if (match) versions.push({ major: Number(match[1]), minor: Number(match[2]) });
  }
  if (versions.length === 0) return '1.0';
  versions.sort((a, b) => (a.major === b.major ? b.minor - a.minor : b.major - a.major));
  return `${versions[0].major}.${versions[0].minor + 1}`;
}

function suggestDesignVersion(attachments, key) {
  const versions = [];
  for (const att of attachments) {
    const name = String(att.filename || '');
    if (!name.startsWith(`${key}-`)) continue;
    const match = name.match(/-(?:测试方案|测试用例)-v(\d+)\.(\d+)\.(?:md|xmind)$/i);
    if (match) versions.push({ major: Number(match[1]), minor: Number(match[2]) });
  }
  if (versions.length === 0) return '1.0';
  versions.sort((a, b) => (a.major === b.major ? b.minor - a.minor : b.major - a.major));
  return `${versions[0].major}.${versions[0].minor + 1}`;
}

function dedupeAttachments(attachments) {
  const groups = new Map();
  for (const att of attachments) {
    const name = String(att.filename || '').toLowerCase();
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(att);
  }
  const active = new Set();
  const superseded = new Set();
  for (const list of groups.values()) {
    list.sort((a, b) => {
      const byCreated = String(b.created || '').localeCompare(String(a.created || ''));
      if (byCreated !== 0) return byCreated;
      return Number(b.id) - Number(a.id);
    });
    active.add(String(list[0].id));
    for (const old of list.slice(1)) superseded.add(String(old.id));
  }
  return { active, superseded };
}

function pickFromTier(list, kind) {
  const named = list.filter((item) => hasPrdName(item.filename));
  const pool = named.length ? named : list;
  pool.sort((a, b) => {
    const byCreated = String(b.created || '').localeCompare(String(a.created || ''));
    if (byCreated !== 0) return byCreated;
    const byId = Number(b.id) - Number(a.id);
    if (byId !== 0) return byId;
    return String(a.memberPath || '').localeCompare(String(b.memberPath || ''));
  });
  if (pool.length > 1) {
    const top = pool[0];
    const conflicting = pool.filter(
      (item) => String(item.created || '') === String(top.created || '') && String(item.id) !== String(top.id),
    );
    if (conflicting.length > 0) {
      throw new QaError(
        `主需求材料不唯一：${pool.slice(0, 3).map((item) => item.key).join('、')}；请用 --primary 指定`,
        EXIT.PRECONDITION,
      );
    }
  }
  return { ...pool[0], primaryKind: kind };
}

function pickPrimary(materials, explicit) {
  const active = materials.filter(
    (material) =>
      material.status === 'active' &&
      material.role &&
      !ACTIVE_ROLE_EXCLUDES.has(material.role),
  );
  if (explicit) {
    const found = active.find((material) => material.key === explicit || String(material.id) === String(explicit));
    if (!found) {
      throw new QaError(`--primary ${explicit} 不是可用的需求材料。`, EXIT.PRECONDITION);
    }
    const kind =
      found.role === 'prd-md'
        ? 'prd-md'
        : found.role === 'prd-doc'
          ? 'prd-doc'
          : found.role === 'description'
            ? 'description'
            : 'fallback';
    return { ...found, primaryKind: kind };
  }
  const tiers = [
    { roles: ['prd-md'], kind: 'prd-md' },
    { roles: ['prd-doc'], kind: 'prd-doc' },
    { roles: ['description'], kind: 'description' },
  ];
  for (const tier of tiers) {
    const list = active.filter((material) => tier.roles.includes(material.role));
    if (list.length) return pickFromTier(list, tier.kind);
  }
  for (const role of ['overview', 'prototype', 'design', 'template', 'auxiliary']) {
    let list = active.filter((material) => material.role === role);
    // 兜底只接受文档类材料：扫描结果表、截图等数据文件不能当作需求正文。
    if (role === 'auxiliary') {
      list = list.filter((material) => REQUIREMENT_DOC_EXTENSIONS.has(String(path.extname(String(material.filename || ''))).toLowerCase()));
    }
    if (list.length) return pickFromTier(list, 'fallback');
  }
  const unreadable = materials.filter(
    (material) =>
      material.status === 'active' &&
      material.unreadable &&
      !isExcludedRole(material.role),
  );
  if (unreadable.length) return { ...unreadable[0], primaryKind: 'unreadable' };
  return null;
}

function textFileName(prefix, memberPath) {
  const raw = `${prefix}__${String(memberPath || '').replace(/\//g, '__')}`;
  return `${safeFilename(raw, 'material')}.txt`;
}

async function writeExtractedText(extractedDir, prefix, memberPath, text) {
  if (!text || !String(text).trim()) return '';
  const file = path.join(extractedDir, textFileName(prefix, memberPath));
  await writeFileAtomic(file, text);
  return file;
}

async function processArchiveMembers({
  buffer,
  filename,
  attachmentId,
  depth,
  destDir,
  extractedDir,
  created,
  materials,
  archives,
  keyPrefix,
  maxDepth,
}) {
  const inspected = await inspectArchive(buffer, filename);
  archives.push({
    key: keyPrefix,
    id: String(attachmentId),
    filename,
    parentPath: keyPrefix === String(attachmentId) ? '' : keyPrefix.slice(String(attachmentId).length + 1),
    depth,
    format: inspected.format,
    entryCount: inspected.entryCount,
    selected: inspected.selected.length,
    ignored: inspected.ignored,
    truncated: inspected.truncated,
    unreadable: inspected.unreadable,
    encrypted: inspected.encrypted,
    reason: inspected.reason,
  });
  if (inspected.unreadable) return inspected;
  for (const member of inspected.selected) {
    const memberKey = `${keyPrefix}!${member.path}`;
    if (isArchiveExtension(member.path) && depth + 1 < maxDepth) {
      const nestedDir = path.join(destDir, ...member.path.split('/').slice(0, -1));
      await processArchiveMembers({
        buffer: member.data,
        filename: member.path,
        attachmentId,
        depth: depth + 1,
        destDir: nestedDir,
        extractedDir,
        created,
        materials,
        archives,
        keyPrefix: memberKey,
        maxDepth,
      });
      continue;
    }
    const localFile = ensureInside(destDir, path.join(...member.path.split('/')));
    await ensureDir(path.dirname(localFile));
    await writeBinaryAtomic(localFile, member.data);
    const extracted = extractText(basenameOf(member.path), member.data);
    const extractedPath = await writeExtractedText(extractedDir, memberKey, member.path, extracted.text);
    materials.push({
      id: String(attachmentId),
      key: memberKey,
      parentId: String(attachmentId),
      memberPath: member.path,
      filename: basenameOf(member.path),
      sourceFilename: filename,
      localFile,
      size: member.size,
      created,
      role: member.role,
      status: 'active',
      kind: extracted.kind,
      needsRead: Boolean(extracted.needsRead),
      unreadable: Boolean(extracted.unreadable),
      error: extracted.error || '',
      textChars: extracted.text ? extracted.text.length : 0,
      extractedPath,
    });
  }
  return inspected;
}

let skipWithComment = true;

async function skipAndReturn(client, workspace, runId, key, stage, { reason, errorCode, attachments }) {
  let commentId = '';
  const warnings = [];
  const body =
    stage === 'design'
      ? `h3. QA-测试方案不满足执行条件\n*执行结论：不满足执行条件，本次未生成测试方案*\n原因：${reason}\n标记：${errorCode}`
      : `评审不适用\n原因：${reason}\n需求准入门禁未执行，未生成需求准入门禁清单。\n标记：${errorCode}`;
  try {
    if (skipWithComment) {
      const out = await syncStatusComment({ client, key, body, runId, workspace });
      commentId = out.commentId;
    }
  } catch (error) {
    warnings.push(`评论写入失败：${error.message}`);
    process.stderr.write(`[jira-download][WARN] 评论写入失败：${error.message}\n`);
  }
  await writeSkipResult(workspace, runId, {
    jiraKey: key,
    reason,
    errorCode,
    attachments,
    commentId,
    warnings,
    stage,
  });
  process.stdout.write(`SKIPPED=${errorCode}\n`);
}

async function main() {
  checkNode();
  const argv = process.argv.slice(2);
  const args = parseArgs(argv);
  const key = parseIssueKey(args.key || args._[0]);
  const workspace = resolveWorkspace(args.workspace);
  const runId = args['run-id'];
  // 试跑模式：跳过时不在 Jira 写评论，只落本地 result
  skipWithComment = !(args['no-comment'] === true || args['no-comment'] === 'true' || args['dry-run'] === true || args['dry-run'] === 'true');
  if (!runId) {
    process.stderr.write('[jira-download][ERR] 缺少 --run-id；请先运行 jira-read.mjs。\n');
    throw new QaError('参数或配置错误', EXIT.USAGE);
  }
  const stage = stageFromArgv(argv);
  const archiveEnabled = String(process.env.QA_ARCHIVE || 'wasm').toLowerCase() !== 'off';

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

  const componentScope = (issue?.fields?.components || []).map((component) => component.name).filter(Boolean)[0] || '';
  const scope = normalizeScope(args.scope || process.env.QA_SCOPE || componentScope);
  const requirementName = suggestName(issue?.fields?.summary);
  const inputsDir = path.join(workspace, 'inputs', 'prd', ...(scope ? scope.split('/') : []), key);
  const extractedDir = path.join(inputsDir, '_extracted');
  const archivesRoot = path.join(inputsDir, '_archives');
  await ensureDir(extractedDir);

  const { active, superseded } = dedupeAttachments(attachments);
  const activeAttachments = attachments.filter((att) => active.has(String(att.id)));
  const candidates = activeAttachments.filter(
    (att) => isRequirementExtension(att.filename) && !isExcludedRole(classifyRole(att.filename)),
  );

  if (attachments.length === 0 && !description) {
    await skipAndReturn(client, workspace, runId, key, stage, {
      reason: `${key} 没有附件，也没有 Jira 需求描述，无法取得需求材料。`,
      errorCode: 'NO_ATTACHMENTS',
      attachments: [],
    });
    return;
  }
  if (candidates.length === 0 && !description) {
    await skipAndReturn(client, workspace, runId, key, stage, {
      reason: `${key} 没有可识别的需求侧附件，也没有 Jira 需求描述。`,
      errorCode: 'NO_REQUIREMENT_MATERIALS',
      attachments,
    });
    return;
  }

  const materials = [];
  const archives = [];
  const usedNames = new Set();
  const maxDepth = archiveLimits().maxDepth;

  for (const att of attachments) {
    const id = String(att.id);
    const role = classifyRole(att.filename);
    const status = active.has(id) ? 'active' : superseded.has(id) ? 'superseded' : 'ignored';
    const base = {
      id,
      key: id,
      filename: att.filename,
      sourceFilename: att.filename,
      size: att.size,
      created: att.created,
      status,
      memberPath: '',
    };
    if (status === 'superseded') {
      materials.push({ ...base, role: 'superseded', kind: '', needsRead: false, unreadable: false, textChars: 0, localFile: '', extractedPath: '' });
      continue;
    }
    if (isExcludedRole(role)) {
      materials.push({ ...base, role, kind: '', needsRead: false, unreadable: false, textChars: 0, localFile: '', extractedPath: '' });
      continue;
    }
    if (!isRequirementExtension(att.filename)) {
      materials.push({ ...base, role: 'unsupported', kind: 'unsupported', needsRead: false, unreadable: false, textChars: 0, localFile: '', extractedPath: '' });
      continue;
    }

    const isArchive = isArchiveExtension(att.filename);
    const localName = safeFilename(att.filename, 'material');
    let localFile = path.join(inputsDir, localName);
    if (usedNames.has(localName)) localFile = path.join(inputsDir, `${id}-${localName}`);
    usedNames.add(path.basename(localFile));
    const maxMb = isArchive
      ? Math.max(Number(process.env.QA_MAX_ARCHIVE_MB || 100), Number(args['max-mb'] || 0) || 0)
      : Number(args['max-mb'] || 0) || Number(process.env.QA_MAX_ATTACHMENT_MB || 50);
    await client.downloadAttachment(id, localFile, { maxMb });
    const buffer = await readFile(localFile);

    if (isArchive) {
      const inspected = archiveEnabled
        ? await processArchiveMembers({
            buffer,
            filename: att.filename,
            attachmentId: id,
            depth: 0,
            destDir: path.join(archivesRoot, id),
            extractedDir,
            created: att.created,
            materials,
            archives,
            keyPrefix: id,
            maxDepth,
          })
        : {
            unreadable: true,
            truncated: false,
            reason: '压缩包读取已关闭（QA_ARCHIVE=off）',
            format: '',
            entryCount: 0,
            selected: [],
            ignored: {},
          };
      if (!archiveEnabled) {
        archives.push({
          key: id,
          id,
          filename: att.filename,
          parentPath: '',
          depth: 0,
          format: '',
          entryCount: 0,
          selected: 0,
          ignored: {},
          truncated: false,
          unreadable: true,
          encrypted: false,
          reason: inspected.reason,
        });
      }
      materials.push({
        ...base,
        role: 'archive',
        kind: 'archive',
        needsRead: false,
        unreadable: Boolean(inspected.unreadable),
        truncated: Boolean(inspected.truncated),
        error: inspected.reason || '',
        textChars: 0,
        localFile,
        extractedPath: '',
        archiveFormat: inspected.format || '',
        entryCount: inspected.entryCount || 0,
        memberCount: inspected.selected ? inspected.selected.length : 0,
      });
      continue;
    }

    const extracted = extractText(att.filename, buffer);
    const extractedPath = await writeExtractedText(extractedDir, id, '', extracted.text);
    materials.push({
      ...base,
      role,
      kind: extracted.kind,
      needsRead: Boolean(extracted.needsRead),
      unreadable: Boolean(extracted.unreadable),
      error: extracted.error || '',
      textChars: extracted.text ? extracted.text.length : 0,
      localFile,
      extractedPath,
    });
  }

  const hasPrd = materials.some(
    (material) => material.status === 'active' && (material.role === 'prd-md' || material.role === 'prd-doc'),
  );
  const descriptionChars = String(description || '').trim().length;
  // 描述只是兜底：单内已有需求侧附件时，不以描述替代附件充当主材料。
  const hasRequirementSide = materials.some(
    (material) => material.status === 'active' && material.role && !IGNORED_ROLES.has(material.role) && material.role !== 'description',
  );
  if (!hasPrd && !hasRequirementSide && description) {
    const filename = `${key}-需求描述.md`;
    const content = `# ${issue?.fields?.summary || key}\n\n> 来源：Jira 需求字段（该单没有 PRD/需求文档时作为主需求材料）\n\n${description}\n`;
    const localFile = path.join(inputsDir, filename);
    await writeFileAtomic(localFile, content);
    const extractedPath = await writeExtractedText(extractedDir, 'issue-description', '', content);
    materials.push({
      id: 'issue-description',
      key: 'issue-description',
      filename,
      sourceFilename: '',
      localFile,
      size: Buffer.byteLength(content),
      created: issue?.fields?.updated || '',
      role: 'description',
      status: 'active',
      kind: 'text',
      needsRead: false,
      unreadable: false,
      textChars: content.length,
      extractedPath,
      source: 'jira-description',
      memberPath: '',
    });
  }

  const primary = pickPrimary(materials, args.primary);
  if (!primary) {
    await skipAndReturn(client, workspace, runId, key, stage, {
      reason: '找不到可用的主需求材料（需求 MD / docx/xlsx PRD / Jira 需求描述），也没有可用的 HTML 兜底；需人工指定 --primary。',
      errorCode: 'NO_PRIMARY_MATERIAL',
      attachments,
    });
    return;
  }

  const activeMaterials = materials.filter((material) => material.status === 'active');
  const result = {
    schemaVersion: 2,
    key,
    runId,
    scope,
    requirementName,
    inputsDir,
    descriptionChars,
    primaryId: primary ? String(primary.id) : '',
    primaryKey: primary ? String(primary.key) : '',
    primaryKind: primary ? String(primary.primaryKind || '') : '',
    primaryFilename: primary ? primary.filename : '',
    primaryMemberPath: primary ? String(primary.memberPath || '') : '',
    primaryFallback: Boolean(primary && primary.primaryKind === 'fallback'),
    suggestedDeliverableVersion: suggestDeliverableVersion(attachments, key),
    suggestedDesignVersion: suggestDesignVersion(attachments, key),
    materials,
    archives,
    counts: {
      total: materials.length,
      active: activeMaterials.length,
      requirementSide: activeMaterials.filter((material) => !['archive'].includes(material.role)).length,
      archiveMembers: activeMaterials.filter((material) => Boolean(material.memberPath)).length,
      ignored: materials.filter((material) => material.status !== 'active').length,
    },
  };
  const runDir = path.join(workspace, '.qa', 'runs', runId);
  await writeJson(path.join(runDir, 'materials.json'), result);
  log('jira-download', `scope=${scope || '(empty)'} primary=${result.primaryKey || '(none)'} ${result.primaryFilename}`);
  log('jira-download', `materials=${materials.length} archives=${archives.length} inputs=${inputsDir}`);
  process.stdout.write(
    `SCOPE=${scope}\n` +
      `REQUIREMENT_NAME=${requirementName}\n` +
      `PRIMARY_ID=${result.primaryId}\n` +
      `PRIMARY_KEY=${result.primaryKey}\n` +
      `PRIMARY_KIND=${result.primaryKind}\n` +
      `MATERIALS=${materials.length}\n` +
      `ARCHIVES=${archives.length}\n` +
      `SUGGESTED_DESIGN_VERSION=${result.suggestedDesignVersion}\n` +
      `INPUTS_DIR=${inputsDir}\n`,
  );
}

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: stageFromArgv(process.argv.slice(2)), script: 'jira-download' }),
);
