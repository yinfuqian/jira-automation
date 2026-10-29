#!/usr/bin/env node
/**
 * 把 materials.json 汇总成给模型阅读的材料摘要。
 * 不改变原文；只做拼接、标注与截断（截断处显式声明）。
 */

import path from 'node:path';

import { EXIT, QaError, checkNode, log, parseArgs, resolveWorkspace } from './lib/env.mjs';
import { readJson, readText, writeFileAtomic } from './lib/fs-safe.mjs';
import { handleFatal } from './lib/fatal.mjs';

import { IGNORED_ROLES } from './lib/material-set.mjs';

/** 只给摘要的角色：这两类不参与冲突判定，逐项核对时用 Read 打开原文件。 */
const SUMMARY_ROLES = new Set(['prototype', 'auxiliary']);

function summaryChars() {
  const value = Number(process.env.QA_SUMMARY_CHARS);
  return Number.isFinite(value) && value > 0 ? value : 1500;
}

function isActive(material) {
  return (material.status === undefined || material.status === 'active') && !IGNORED_ROLES.has(material.role);
}

function isArchive(material) {
  return material.role === 'archive';
}

function requirementHint(material) {
  if (material.role === 'unsupported') return '不支持的材料类型，未读取（不作为准入依据）';
  if (SUMMARY_ROLES.has(material.role)) return `仅给前 ${summaryChars()} 字摘要（不参与冲突判定，核对时用 Read 打开原文件）`;
  if (material.needsRead) return '用 Read 工具直接查看原文件';
  if (material.unreadable) return `不可读：${material.error || '抽取失败'}；必须用 Read 查看，仍不可读则记为材料不可读`;
  if (material.truncated) return '读取被限额截断，未完整读取';
  if (material.textChars > 0) return '阅读下方抽取正文';
  return '空文件或无可读正文，需确认';
}

function displayId(material) {
  if (material.id === 'issue-description') return 'Jira 字段';
  return String(material.key || material.id || '');
}

function displayFile(material) {
  if (material.memberPath) return `${material.sourceFilename || material.id}!/${material.memberPath}`;
  return material.filename || '';
}

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const workspace = resolveWorkspace(args.workspace);
  const runId = args['run-id'];
  if (!runId) {
    process.stderr.write('[materials][ERR] 缺少 --run-id。\n');
    throw new QaError('参数或配置错误', EXIT.USAGE);
  }
  const runDir = path.join(workspace, '.qa', 'runs', runId);
  const materials = await readJson(path.join(runDir, 'materials.json'));
  const maxChars = Number(args['max-chars'] || 30000);
  const maxTotalChars = Number(args['max-total-chars'] || 200000);
  const all = Array.isArray(materials.materials) ? materials.materials : [];
  const active = all.filter(isActive);
  const primaryKey = String(materials.primaryKey || materials.primaryId || '');

  const lines = [
    `# 需求材料摘要（${materials.key}）`,
    '',
    `- 工作区：${workspace}`,
    `- 主材料：${primaryKey || '(未确定)'}（${materials.primaryKind || ''}）`,
    `- 主需求附件 ID：${materials.primaryId || ''}`,
    `- 建议需求名称：${materials.requirementName}`,
    `- scope：${materials.scope || '(空)'}`,
    `- 建议门禁版本：${materials.suggestedDeliverableVersion || '1.0'}`,
    `- 建议测试方案版本：${materials.suggestedDesignVersion || '1.0'}`,
    '',
    '## 材料清单',
    '',
    '| ID / 键 | 文件 | 角色 | 类型 | 可抽取字符 | 处理要求 |',
    '|---|---|---|---|---|---|',
  ];
  for (const material of active.filter((item) => !isArchive(item))) {
    const marker = String(material.key || material.id) === primaryKey ? ' ← 主材料' : '';
    lines.push(
      `| ${displayId(material)}${marker} | ${displayFile(material)} | ${material.role} | ${material.kind} | ${material.textChars || 0} | ${requirementHint(material)} |`,
    );
  }
  if (active.filter((item) => !isArchive(item)).length === 0) {
    lines.push('| — | — | — | — | 0 | 没有可读取的需求材料 |');
  }

  if (Array.isArray(materials.archives) && materials.archives.length > 0) {
    lines.push('', '## 压缩包', '', '| 附件 | 格式 | 条目 | 已选 | 忽略 | 状态 |', '|---|---|---|---|---|---|');
    for (const archive of materials.archives) {
      const status = archive.unreadable
        ? `不可读：${archive.reason || ''}`
        : archive.truncated
          ? '部分读取（超限）'
          : archive.encrypted
            ? '加密包，未读取'
            : '已读取';
      lines.push(
        `| ${archive.filename}（${archive.id}） | ${archive.format || ''} | ${archive.entryCount || 0} | ${archive.selected || 0} | ${JSON.stringify(archive.ignored || {})} | ${status} |`,
      );
    }
  }

  lines.push('', '## 抽取正文', '');
  let usedChars = 0;
  for (const material of active.filter((item) => !isArchive(item))) {
    const source = material.source === 'jira-description' ? 'Jira 需求字段' : `附件 ${material.id}`;
    const marker = String(material.key || material.id) === primaryKey ? '（主材料）' : '';
    lines.push(`### ${displayFile(material)}（${source}${marker}，${material.role}）`, '');
    if (material.needsRead) {
      lines.push(`> 该文件为 ${material.kind}，未抽取文本。请用 Read 工具打开：${material.localFile}`, '');
      continue;
    }
    if (material.unreadable) {
      lines.push(`> 材料不可读：${material.error || '抽取失败'}。不得声称已覆盖该材料。`, '');
      continue;
    }
    if (!material.extractedPath) {
      lines.push('> 未抽取到正文。若该材料属于需求侧，必须用 Read 直接查看后再判定；仍不可读时记为材料不可读。', '');
      continue;
    }
    let text = await readText(material.extractedPath);
    if (SUMMARY_ROLES.has(material.role)) {
      const limit = summaryChars();
      const head = text.slice(0, limit);
      const rest = text.length - head.length;
      usedChars += head.length;
      lines.push(head, '');
      if (rest > 0) {
        lines.push(
          `[摘要：仅显示前 ${limit} 字符，原文共 ${text.length} 字符；核对页面控件、用户可见文案或冲突时必须用 Read 打开 ${material.localFile}，不得只凭摘要判定]`,
          '',
        );
      }
      continue;
    }
    if (usedChars >= maxTotalChars) {
      lines.push(`> [已达到本次摘要总预算 ${maxTotalChars} 字符，剩余材料请用 Read 打开原文件]`, '');
      continue;
    }
    const remaining = Math.max(0, maxTotalChars - usedChars);
    const limit = Math.min(maxChars, remaining);
    if (text.length > limit) {
      text = `${text.slice(0, limit)}\n\n[已截断：原文共 ${text.length} 字符，仅显示前 ${limit} 字符；必须用 Read 打开 ${material.localFile} 补读]`;
    }
    usedChars += Math.min(text.length, limit);
    lines.push(text, '');
  }

  const excluded = all.filter((material) => material.status !== 'active' || IGNORED_ROLES.has(material.role));
  if (excluded.length) {
    lines.push('## 已排除（不作为需求材料）', '');
    for (const material of excluded) {
      const reason = material.status === 'superseded' ? '同名旧版' : material.role;
      lines.push(`- ${material.filename}（${material.id}，${reason}）`);
    }
    lines.push('');
  }

  const out = path.join(runDir, 'materials-digest.md');
  const body = lines.join('\n');
  await writeFileAtomic(out, body);
  log('materials', `digest=${out} chars=${body.length} totalBudget=${maxTotalChars}`);
  process.stdout.write(`DIGEST=${out}\n`);
}

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: parseArgs(process.argv.slice(2)).stage || '', script: 'materials' }),
);
