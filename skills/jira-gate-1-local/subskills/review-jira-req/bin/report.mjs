#!/usr/bin/env node
/**
 * 合并规则校验结果与语义评审结果，生成 Markdown 报告 + 终端摘要。
 *
 * 用法：
 *   node scripts/report.mjs --run-id <id> [--out <dir>] [--reports-dir <dir>] [--text-only]
 *
 * 输入（均由前序步骤落盘）：
 *   reports/.work/<run-id>/meta.json       拉取元信息（来源、JQL、范围、截断）
 *   reports/.work/<run-id>/rulecheck.json  规则校验结果
 *   reports/.work/<run-id>/semantic/<KEY>.json  语义评审结果（可选，缺失时如实标注）
 *
 * 输出：
 *   reports/review-report-<timestamp>.md   不覆盖历史报告（specs → 报告已存在时的处理）
 */

import { readFile, writeFile, readdir, mkdir } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';

import { createLogger, makeTimestamp, redactUrl } from './lib/shared.mjs';
import { countBySeverity } from './lib/rules.mjs';
import { formatBytes } from './lib/attachments.mjs';
import { SEMANTIC_DIMENSIONS, SEMANTIC_CONCLUSIONS, validateSemantic } from './lib/semantic-schema.mjs';

/**
 * 默认输出目录基于 cwd，而不是脚本自身位置。
 * 这样脚本被复制进 skill（~/.claude/skills/.../bin/）后，产物仍落在使用者的项目里，
 * 而不是写进 skill 安装目录。
 */
const DEFAULT_WORK_DIR = path.join(process.cwd(), 'reports', '.work');
const DEFAULT_REPORTS_DIR = path.join(process.cwd(), 'reports');

/** 语义评审必须覆盖的维度、结论取值与结构校验（与自检脚本共用一份契约）。 */
export { SEMANTIC_DIMENSIONS, SEMANTIC_CONCLUSIONS, validateSemantic };

/** 结论排序权重：需修改与不合格优先（specs → 批量评审汇总）。 */
const CONCLUSION_WEIGHT = { 不合格: 0, 需修改: 1, 合格: 2 };

export function parseArgs(argv) {
  const args = { runId: null, workDir: DEFAULT_WORK_DIR, reportsDir: DEFAULT_REPORTS_DIR, textOnly: false, now: null };
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    const next = argv[i + 1];
    if (token === '--run-id') {
      args.runId = next;
      i += 1;
    } else if (token === '--out') {
      args.workDir = next;
      i += 1;
    } else if (token === '--reports-dir') {
      args.reportsDir = next;
      i += 1;
    } else if (token === '--text-only') {
      args.textOnly = true;
    } else {
      throw new Error(`无法识别的参数 "${token}"。`);
    }
  }
  if (!args.runId) throw new Error('需要 --run-id <id>。');
  return args;
}

/** 加载一轮运行的全部输入。 */
export async function loadRunData({ runId, workDir = DEFAULT_WORK_DIR }) {
  const runDir = path.join(workDir, runId);
  if (!existsSync(runDir)) {
    throw new Error(`找不到运行目录 ${runDir}。请先执行 jira-fetch.mjs 完成拉取。`);
  }

  const metaPath = path.join(runDir, 'meta.json');
  const rulecheckPath = path.join(runDir, 'rulecheck.json');
  if (!existsSync(rulecheckPath)) {
    throw new Error(`缺少 ${rulecheckPath}。请先执行 rulecheck.mjs --run-id ${runId}。`);
  }

  const meta = existsSync(metaPath) ? JSON.parse(await readFile(metaPath, 'utf8')) : null;
  const rulecheck = JSON.parse(await readFile(rulecheckPath, 'utf8'));
  const semantic = await loadSemantic(runDir);
  const raw = await loadRawSummaries(runDir);

  return { runDir, meta, rulecheck, semantic, raw };
}

/** 读取 semantic/<KEY>.json；文件缺失或字段不全时如实记录，不臆造内容。 */
async function loadSemantic(runDir) {
  const dir = path.join(runDir, 'semantic');
  const byKey = new Map();
  const problems = [];

  if (!existsSync(dir)) return { byKey, problems, missingAll: true };

  const files = (await readdir(dir)).filter((name) => name.endsWith('.json')).sort();
  for (const file of files) {
    const key = file.replace(/\.json$/, '');
    try {
      const parsed = JSON.parse(await readFile(path.join(dir, file), 'utf8'));
      const validation = validateSemantic(parsed);
      if (validation.length > 0) {
        problems.push({ key, file, issues: validation });
        continue;
      }
      byKey.set(key, parsed);
    } catch (error) {
      problems.push({ key, file, issues: [`JSON 解析失败：${error.message}`] });
    }
  }

  return { byKey, problems, missingAll: false };
}

/** 读取 raw/<KEY>.json，供报告展示标题与原始描述引用。 */
async function loadRawSummaries(runDir) {
  const dir = path.join(runDir, 'raw');
  const byKey = new Map();
  if (!existsSync(dir)) return byKey;

  const files = (await readdir(dir)).filter((name) => name.endsWith('.json')).sort();
  for (const file of files) {
    try {
      const parsed = JSON.parse(await readFile(path.join(dir, file), 'utf8'));
      if (parsed?.key) byKey.set(parsed.key, parsed);
    } catch {
      // raw 文件损坏时不影响报告主体，标题回退为 Key
    }
  }
  return byKey;
}

/** 组装报告数据模型。 */
export function buildModel({ runId, meta, rulecheck, semantic, raw, generatedAt }) {
  const items = rulecheck.results.map((result) => {
    const key = result.label;
    const rawIssue = raw.get(key) ?? null;
    const semanticResult = semantic.byKey.get(key) ?? null;
    return {
      key,
      summary: rawIssue?.summary ?? '',
      status: rawIssue?.status ?? '',
      issueType: rawIssue?.issueType ?? '',
      descriptionRaw: rawIssue?.descriptionRaw ?? '',
      draftPath: path.join('reports', '.work', runId, 'normalized', `${key}.spec.md`),
      ruleResult: result,
      ruleCounts: countBySeverity(result.allIssues),
      semantic: semanticResult,
      attachments: enrichAttachments(rawIssue?.attachments, semanticResult),
    };
  });

  items.sort(compareItems);

  const unreachable = (meta?.failures ?? []).map((failure) => ({
    key: failure.key,
    kind: failure.kind,
    message: failure.message,
  }));

  return {
    runId,
    generatedAt,
    meta,
    items,
    unreachable,
    missingDrafts: rulecheck.missingDrafts ?? [],
    semanticProblems: semantic.problems ?? [],
    semanticMissingAll: Boolean(semantic.missingAll),
    attachmentSummary: summarizeAttachments(items),
    summary: summarizeItems(items),
  };
}

/** 把 fetch 侧附件清单与模型侧的检查状态并列。模型未记录时保持 null，不臆测。 */
function enrichAttachments(attachments, semanticResult) {
  if (!Array.isArray(attachments)) return [];
  const recorded = new Map();
  if (Array.isArray(semanticResult?.attachments)) {
    for (const entry of semanticResult.attachments) {
      if (entry?.id !== undefined && entry?.id !== null) recorded.set(String(entry.id), entry.status);
    }
  }
  return attachments.map((attachment) => ({
    ...attachment,
    modelStatus: recorded.get(String(attachment?.id)) ?? null,
  }));
}

const FETCH_UNINSPECTED_STATUSES = new Set(['failed', 'skipped', 'unsupported']);

/** 附件总览：fetch 状态与模型检查状态分开统计，缺失时如实标为未记录。 */
export function summarizeAttachments(items) {
  const counts = {
    total: 0,
    text: 0,
    image: 0,
    unsupported: 0,
    skipped: 0,
    failed: 0,
    modelUsed: 0,
    modelNotUsed: 0,
    modelNotInspected: 0,
    modelUnrecorded: 0,
  };
  const unchecked = [];
  const duplicates = [];

  for (const item of items) {
    for (const attachment of item.attachments ?? []) {
      counts.total += 1;
      if (Object.hasOwn(counts, attachment.status)) counts[attachment.status] += 1;

      if (attachment.modelStatus === 'used') counts.modelUsed += 1;
      else if (attachment.modelStatus === 'not-used') counts.modelNotUsed += 1;
      else if (attachment.modelStatus === 'not-inspected') counts.modelNotInspected += 1;
      else counts.modelUnrecorded += 1;

      if (attachment.status === 'skipped' && /^重复附件：/.test(attachment.note || '')) {
        duplicates.push({
          key: item.key,
          id: attachment.id,
          filename: attachment.filename,
          modelStatus: attachment.modelStatus ?? null,
          reason: attachment.note,
        });
      } else if (FETCH_UNINSPECTED_STATUSES.has(attachment.status)) {
        unchecked.push({
          key: item.key,
          id: attachment.id,
          filename: attachment.filename,
          reason: attachment.note || `fetch 状态：${attachment.status}`,
        });
      } else if (!attachment.modelStatus) {
        unchecked.push({
          key: item.key,
          id: attachment.id,
          filename: attachment.filename,
          reason: '模型检查状态未记录',
        });
      } else if (attachment.modelStatus === 'not-inspected') {
        unchecked.push({
          key: item.key,
          id: attachment.id,
          filename: attachment.filename,
          reason: '模型明确未检查',
        });
      }
    }
  }

  return { counts, unchecked, duplicates };
}

/** 需修改与不合格优先；同档内按 Key 排序，保证输出稳定。 */
function compareItems(a, b) {
  const weightA = CONCLUSION_WEIGHT[a.ruleResult.conclusion] ?? 9;
  const weightB = CONCLUSION_WEIGHT[b.ruleResult.conclusion] ?? 9;
  if (weightA !== weightB) return weightA - weightB;
  return a.key.localeCompare(b.key);
}

export function summarizeItems(items) {
  const conclusions = { 合格: 0, 需修改: 0, 不合格: 0 };
  const ruleFailures = new Map();
  const issueTotals = { ERROR: 0, WARNING: 0, INFO: 0, total: 0 };

  for (const item of items) {
    conclusions[item.ruleResult.conclusion] = (conclusions[item.ruleResult.conclusion] ?? 0) + 1;
    for (const issue of item.ruleResult.allIssues) {
      ruleFailures.set(issue.ruleId, (ruleFailures.get(issue.ruleId) ?? 0) + 1);
      issueTotals[issue.severity] += 1;
      issueTotals.total += 1;
    }
  }

  return {
    specsChecked: items.length,
    conclusions,
    issueTotals,
    ruleFailureRanking: [...ruleFailures.entries()]
      .map(([ruleId, count]) => ({ ruleId, count }))
      .sort((a, b) => b.count - a.count || a.ruleId.localeCompare(b.ruleId)),
  };
}

/** 终端摘要（specs → 终端输出结论摘要）。 */
export function formatTerminalSummary(model) {
  const lines = [];
  lines.push(`评审完成：${model.summary.specsChecked} 条需求`);
  lines.push(
    `结论分布：合格 ${model.summary.conclusions['合格']} / ` +
      `需修改 ${model.summary.conclusions['需修改']} / ` +
      `不合格 ${model.summary.conclusions['不合格']}`,
  );
  lines.push('');

  for (const item of model.items) {
    const counts = item.ruleCounts;
    lines.push(
      `  ${item.key}  ${item.summary || '(无标题)'}\n` +
        `      结论：${item.ruleResult.conclusion}   ` +
        `规则 ${item.ruleResult.passedRuleCount}/${item.ruleResult.rulesEvaluated} 通过   ` +
        `问题 ${counts.total} 个（ERROR ${counts.ERROR} / WARNING ${counts.WARNING} / INFO ${counts.INFO}）`,
    );
  }

  for (const item of model.unreachable) {
    lines.push(`  ${item.key}  (无法访问：${item.kind})`);
  }

  const attachmentCounts = model.attachmentSummary?.counts;
  if (attachmentCounts && attachmentCounts.total > 0) {
    const duplicateCount = model.attachmentSummary.duplicates.length;
    lines.push('');
    lines.push(
      `附件：${attachmentCounts.total} 个（文本 ${attachmentCounts.text} / 图片 ${attachmentCounts.image} / ` +
        `无法解析 ${attachmentCounts.unsupported} / 跳过 ${attachmentCounts.skipped} / ` +
        `失败 ${attachmentCounts.failed}），` +
        `${model.attachmentSummary.unchecked.length} 个未能检查或未记录` +
        (duplicateCount > 0 ? `，${duplicateCount} 个内容重复` : ''),
    );
  }

  if (model.summary.ruleFailureRanking.length > 0) {
    lines.push('');
    lines.push('规则失败次数排序：');
    for (const entry of model.summary.ruleFailureRanking) {
      lines.push(`  ${entry.count} 次  ${entry.ruleId}`);
    }
  }

  return lines.join('\n');
}

/** 生成 Markdown 报告正文。 */
export function renderMarkdown(model) {
  const out = [];
  const meta = model.meta ?? {};

  out.push('# 需求评审报告');
  out.push('');
  out.push('| 项 | 值 |');
  out.push('| --- | --- |');
  out.push(`| 运行 id | \`${model.runId}\` |`);
  out.push(`| 运行时间 | ${model.generatedAt} |`);
  out.push(`| JIRA 来源 | ${redactUrl(meta.baseUrl ?? '(未知)')} |`);
  out.push(`| 拉取模式 | ${describeMode(meta)} |`);
  if (meta.jql) out.push(`| JQL | \`${meta.jql}\` |`);
  out.push(`| 评审范围 | ${model.summary.specsChecked} 条 |`);
  if (meta.truncated) {
    out.push(
      `| 截断声明 | JQL/Key 共命中 ${meta.total} 条，本次仅评审上限内 ${meta.fetched} 条，` +
        `被截断 ${meta.truncatedCount} 条 |`,
    );
  }
  out.push('| 规则基线 | openspec 1.12.0 |');
  out.push(
    `| 规则结论依据 | 归一化草稿（\`reports/.work/${model.runId}/normalized/\`），` +
      '草稿由模型从 JIRA 描述映射而来，可人工修正后用 `rulecheck.mjs --only-rules` 复跑 |',
  );
  out.push('');

  out.push('## 汇总');
  out.push('');
  out.push('| 结论 | 数量 |');
  out.push('| --- | --- |');
  out.push(`| 不合格 | ${model.summary.conclusions['不合格']} |`);
  out.push(`| 需修改 | ${model.summary.conclusions['需修改']} |`);
  out.push(`| 合格 | ${model.summary.conclusions['合格']} |`);
  out.push('');
  out.push(
    `问题总数：${model.summary.issueTotals.total} ` +
      `（ERROR ${model.summary.issueTotals.ERROR} / ` +
      `WARNING ${model.summary.issueTotals.WARNING} / ` +
      `INFO ${model.summary.issueTotals.INFO}）`,
  );
  out.push('');

  if (model.summary.ruleFailureRanking.length > 0) {
    out.push('规则失败次数排序：');
    out.push('');
    model.summary.ruleFailureRanking.forEach((entry, index) => {
      out.push(`${index + 1}. \`${entry.ruleId}\` — ${entry.count} 次`);
    });
    out.push('');
  }

  renderAttachmentOverview(out, model.attachmentSummary);

  if (model.unreachable.length > 0) {
    out.push('### 无法访问的需求');
    out.push('');
    for (const item of model.unreachable) {
      out.push(`- \`${item.key}\`（${item.kind}）：${item.message}`);
    }
    out.push('');
  }

  if (model.missingDrafts.length > 0) {
    out.push('### 缺少归一化草稿');
    out.push('');
    out.push(`以下 issue 未生成草稿，因此没有规则结论：${model.missingDrafts.map((k) => `\`${k}\``).join('、')}`);
    out.push('');
  }

  if (model.semanticProblems.length > 0) {
    out.push('### 语义评审结果不完整');
    out.push('');
    out.push('以下语义评审结果结构不合规，已跳过渲染（未臆造内容）：');
    out.push('');
    for (const problem of model.semanticProblems) {
      out.push(`- \`${problem.key}\`：${problem.issues.join('；')}`);
    }
    out.push('');
  }

  out.push('## 需求逐条');
  out.push('');

  for (const item of model.items) {
    out.push(`### ${item.key} ${item.summary || '(无标题)'}`);
    out.push('');
    out.push(`- 结论：**${item.ruleResult.conclusion}**`);
    out.push(`- 类型 / 状态：${item.issueType || '(未知)'} / ${item.status || '(未知)'}`);
    out.push(`- 归一化草稿：\`${item.draftPath}\``);
    out.push(
      `- 规则校验：${item.ruleResult.passedRuleCount}/${item.ruleResult.rulesEvaluated} 通过，` +
        `需求 ${item.ruleResult.requirements.length} 条，问题 ${item.ruleCounts.total} 个` +
        `（ERROR ${item.ruleCounts.ERROR} / WARNING ${item.ruleCounts.WARNING} / INFO ${item.ruleCounts.INFO}）`,
    );
    out.push('');

    if (item.ruleResult.allIssues.length > 0) {
      out.push('#### 规则问题');
      out.push('');
      item.ruleResult.allIssues.forEach((issue, index) => {
        out.push(`${index + 1}. **[${issue.severity}] \`${issue.ruleId}\`** — ${issue.summary}`);
        out.push(`   - 所属需求：${issue.requirement ?? '(文档级)'}`);
        out.push(`   - 位置：${describePosition(issue)}`);
        if (issue.evidence) {
          out.push(`   - 原文片段：\`${escapeInline(issue.evidence)}\``);
        }
        if (issue.fix.includes('\n')) {
          out.push('   - 建议：');
          out.push('     ```');
          for (const line of issue.fix.split('\n')) {
            out.push(line === '' ? '     ' : `     ${line}`);
          }
          out.push('     ```');
        } else {
          out.push(`   - 建议：${issue.fix}`);
        }
      });
      out.push('');
    } else {
      out.push('规则校验全部通过。');
      out.push('');
    }

    out.push('#### 语义评审');
    out.push('');
    if (!item.semantic) {
      out.push('（未产出语义评审结果。）');
      out.push('');
    } else {
      out.push('| 维度 | 结论 | 理由 |');
      out.push('| --- | --- | --- |');
      for (const dimension of item.semantic.dimensions) {
        const name =
          SEMANTIC_DIMENSIONS.find((d) => d.id === dimension.id)?.name ?? dimension.id;
        out.push(`| ${name} | ${dimension.conclusion} | ${escapeCell(dimension.reasons.join('；'))} |`);
      }
      out.push('');

      const evidenceRows = item.semantic.dimensions.flatMap((dimension) =>
        (dimension.evidence ?? []).map((evidence) => ({ dimension, evidence })),
      );
      if (evidenceRows.length > 0) {
        out.push('原文引用与建议：');
        out.push('');
        for (const { dimension, evidence } of evidenceRows) {
          const name = SEMANTIC_DIMENSIONS.find((d) => d.id === dimension.id)?.name ?? dimension.id;
          out.push(`- （${name}）\`${escapeInline(evidence.quote)}\``);
          if (evidence.why) out.push(`  - 问题：${evidence.why}`);
          if (evidence.rewrite) out.push(`  - 建议改写：${evidence.rewrite}`);
        }
        out.push('');
      }

      for (const dimension of item.semantic.dimensions) {
        if (dimension.conclusion === '无法判定' && dimension.missingInformation) {
          out.push(`- 无法判定项（${dimension.id}）：缺少 ${dimension.missingInformation}`);
        }
      }

      if (Array.isArray(item.semantic.missingScenarios) && item.semantic.missingScenarios.length > 0) {
        out.push('');
        out.push(`缺失的场景类别：${item.semantic.missingScenarios.join('、')}`);
      }

      if (Array.isArray(item.semantic.suggestedScenarios) && item.semantic.suggestedScenarios.length > 0) {
        out.push('');
        out.push('建议补充的场景：');
        out.push('');
        for (const scenario of item.semantic.suggestedScenarios) {
          out.push(`- #### Scenario: ${scenario.title}`);
          out.push(`  - **WHEN** ${scenario.when}`);
          out.push(`  - **THEN** ${scenario.then}`);
        }
      }

      if (item.semantic.nonAtomic?.detected) {
        out.push('');
        out.push('非原子需求，建议拆分为：');
        out.push('');
        for (const split of item.semantic.nonAtomic.suggestedSplit ?? []) {
          out.push(`- ${split}`);
        }
      }
      out.push('');
    }

    renderItemAttachments(out, item);
  }

  return `${out.join('\n')}\n`;
}

const ATTACHMENT_STATUS_LABELS = {
  text: '已抽取文本',
  image: '图片',
  unsupported: '无法解析',
  skipped: '已跳过',
  failed: '失败',
};

const MODEL_STATUS_LABELS = {
  used: '已使用',
  'not-used': '未使用',
  'not-inspected': '未检查',
};

/** 报告开头的附件总览与"未能检查"告警。 */
function renderAttachmentOverview(out, attachmentSummary) {
  const counts = attachmentSummary?.counts ?? { total: 0 };
  out.push('## 附件检查总览');
  out.push('');

  if (counts.total === 0) {
    out.push('本次运行未发现附件。');
    out.push('');
    return;
  }

  out.push(
    `本次共 ${counts.total} 个附件：文本 ${counts.text} / 图片 ${counts.image} / ` +
      `无法解析 ${counts.unsupported} / 跳过 ${counts.skipped} / 失败 ${counts.failed}。`,
  );
  out.push('');
  const duplicateNotInspected = (attachmentSummary.duplicates ?? []).filter(
    (entry) => entry.modelStatus === 'not-inspected',
  ).length;
  out.push(
    `模型检查状态：已使用 ${counts.modelUsed} / 未使用 ${counts.modelNotUsed} / ` +
      `未检查 ${counts.modelNotInspected}` +
      (duplicateNotInspected > 0 ? `（其中 ${duplicateNotInspected} 个为内容重复附件）` : '') +
      ` / 未记录 ${counts.modelUnrecorded}。`,
  );
  out.push('');

  if (attachmentSummary.unchecked.length > 0) {
    out.push('### 未能检查的附件');
    out.push('');
    out.push('以下附件未能下载、解析，或模型未记录检查状态。它们不影响规则结论，但评审可能因此不完整：');
    out.push('');
    for (const entry of attachmentSummary.unchecked) {
      const id = entry.id ? `（#${entry.id}）` : '';
      out.push(`- \`${entry.key}\` / ${entry.filename}${id}：${entry.reason}`);
    }
    out.push('');
  }

  if (attachmentSummary.duplicates.length > 0) {
    out.push('### 内容重复的附件');
    out.push('');
    out.push('以下附件与同 issue 内其他附件内容相同，已按 SHA-256 去重，评审覆盖以原附件为准：');
    out.push('');
    for (const entry of attachmentSummary.duplicates) {
      const id = entry.id ? `（#${entry.id}）` : '';
      out.push(`- \`${entry.key}\` / ${entry.filename}${id}：${entry.reason}`);
    }
    out.push('');
  }
}

/** 每条需求下的附件检查明细：fetch 状态与模型检查状态并列。 */
function renderItemAttachments(out, item) {
  out.push('#### 附件检查');
  out.push('');

  if (!item.attachments || item.attachments.length === 0) {
    out.push('（该 issue 无附件。）');
    out.push('');
    return;
  }

  out.push('| 文件 | 类型 | 大小 | 下载/抽取 | 模型检查 | 抽取字符 | 备注 |');
  out.push('| --- | --- | --- | --- | --- | --- | --- |');

  for (const attachment of item.attachments) {
    const status =
      ATTACHMENT_STATUS_LABELS[attachment.status] ?? attachment.status ?? '(未知)';
    const modelStatus = attachment.modelStatus
      ? MODEL_STATUS_LABELS[attachment.modelStatus] ?? attachment.modelStatus
      : '未记录';
    const chars =
      attachment.status === 'text'
        ? `${attachment.textChars ?? 0}${attachment.truncated ? '（已截断）' : ''}`
        : '—';
    out.push(
      `| ${escapeCell(attachment.filename)}${attachment.id ? `（#${escapeCell(String(attachment.id))}）` : ''} | ` +
        `${escapeCell(attachment.mimeType || '(未知)')} | ` +
        `${formatBytes(attachment.size)} | ${escapeCell(status)} | ${escapeCell(modelStatus)} | ` +
        `${escapeCell(chars)} | ${escapeCell(attachment.note || '')} |`,
    );
  }

  out.push('');
}

/** 渲染问题的位置描述。文档级问题没有行号，不能渲染成"第 null 行"。 */
function describePosition(issue) {
  const parts = [];
  if (Number.isInteger(issue.line) && issue.line > 0) {
    parts.push(`第 ${issue.line} 行`);
  } else {
    parts.push('文档级');
  }
  if (issue.detail) parts.push(issue.detail);
  return parts.join('；');
}

function describeMode(meta) {
  if (meta.mode === 'key') return `单个 Issue（${meta.requestedKey ?? '-'}）`;
  if (meta.mode === 'keys') return `Key 列表（请求 ${(meta.requestedKeys ?? []).length} 条）`;
  if (meta.mode === 'jql') return 'JQL 批量';
  return '(未知)';
}

function escapeCell(text) {
  return String(text ?? '').replace(/\|/g, '\\|').replace(/\n/g, ' ');
}

function escapeInline(text) {
  return String(text ?? '').replace(/`/g, "'").replace(/\n/g, ' ⏎ ');
}

/**
 * 生成报告文件。文件名带时间戳，不覆盖历史报告。
 * @returns {{ model, reportPath }}
 */
export async function generateReport({
  runId,
  workDir = DEFAULT_WORK_DIR,
  reportsDir = DEFAULT_REPORTS_DIR,
  now = new Date(),
  textOnly = false,
} = {}) {
  const data = await loadRunData({ runId, workDir });
  const model = buildModel({ runId, ...data, generatedAt: now.toISOString() });
  const markdown = renderMarkdown(model);

  if (textOnly) return { model, markdown, reportPath: null };

  await mkdir(reportsDir, { recursive: true });
  const reportPath = path.join(reportsDir, `review-report-${makeTimestamp(now)}.md`);
  await writeFile(reportPath, markdown, 'utf8');

  return { model, markdown, reportPath };
}

async function main() {
  const logger = createLogger({});
  let args;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (error) {
    logger.error(`${error.message}\n用法：node scripts/report.mjs --run-id <id> [--out <dir>] [--text-only]`);
    process.exitCode = 4;
    return;
  }

  try {
    const { model, reportPath } = await generateReport({
      runId: args.runId,
      workDir: args.workDir,
      reportsDir: args.reportsDir,
      textOnly: args.textOnly,
    });

    logger.out(formatTerminalSummary(model));

    if (args.textOnly) {
      logger.info('（--text-only：未写出报告文件）');
      return;
    }

    logger.out('');
    logger.out(`报告已写入：${reportPath}`);
    if (model.semanticMissingAll) {
      logger.warn('本次没有找到任何语义评审结果，报告中语义部分会显示为未产出。');
    }
    if (model.semanticProblems.length > 0) {
      logger.warn(`有 ${model.semanticProblems.length} 份语义评审结果结构不合规，已跳过渲染。`);
    }
  } catch (error) {
    logger.error(error.message);
    process.exitCode = 5;
  }
}

if (process.argv[1] && process.argv[1].endsWith('report.mjs')) {
  await main();
}
