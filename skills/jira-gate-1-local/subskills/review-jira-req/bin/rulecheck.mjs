#!/usr/bin/env node
/**
 * 对归一化草稿跑确定性规则校验，输出机器可读 JSON。
 *
 * 用法：
 *   node scripts/rulecheck.mjs --run-id <id> [--out <dir>]     # 校验整轮运行的全部草稿
 *   node scripts/rulecheck.mjs --spec <file> [--json|--text]   # 校验单个草稿（复跑入口）
 *   node scripts/rulecheck.mjs --list-rules                    # 打印规则表
 *
 * `--only-rules` 是显式声明"只跑规则校验"的开关：跳过归一化与语义评审。
 * 本脚本本就只做这件事，该开关用于让人工修正草稿后的复跑意图在命令里可见，
 * 也便于将来在编排层把它与其他步骤区分开。
 *
 * 本脚本不访问网络、不调用模型（specs → 确定性规则校验 → 规则校验与模型解耦）。
 */

import { readFile, writeFile, readdir, mkdir } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { RULES, checkSpec, countBySeverity } from './lib/rules.mjs';
import { createLogger } from './lib/shared.mjs';

/**
 * 默认输出目录基于 cwd，而不是脚本自身位置。
 * 这样脚本被复制进 skill（~/.claude/skills/.../bin/）后，产物仍落在使用者的项目里，
 * 而不是写进 skill 安装目录。
 */
const DEFAULT_OUT = path.join(process.cwd(), 'reports', '.work');
export const RESULT_FILE = 'rulecheck.json';

export function parseArgs(argv) {
  const args = { runId: null, spec: null, outDir: null, format: 'json', listRules: false, onlyRules: false };
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    const next = argv[i + 1];
    if (token === '--run-id') {
      args.runId = next;
      i += 1;
    } else if (token === '--spec') {
      args.spec = next;
      i += 1;
    } else if (token === '--out') {
      args.outDir = next;
      i += 1;
    } else if (token === '--text') {
      args.format = 'text';
    } else if (token === '--json') {
      args.format = 'json';
    } else if (token === '--list-rules') {
      args.listRules = true;
    } else if (token === '--only-rules') {
      args.onlyRules = true;
    } else {
      throw new Error(`无法识别的参数 "${token}"。`);
    }
  }
  if (!args.listRules && !args.runId && !args.spec) {
    throw new Error('需要 --run-id <id> 或 --spec <file> 之一（或用 --list-rules 查看规则表）。');
  }
  return args;
}

/** 校验一个草稿文件，返回结果对象。 */
export async function checkFile(filePath) {
  const content = await readFile(filePath, 'utf8');
  return checkSpec(content, { label: path.basename(filePath, '.spec.md') });
}

/**
 * 校验一轮运行下的全部归一化草稿。
 * 草稿缺失（还没做归一化）时如实报告，不静默跳过。
 */
export async function checkRun({ runId, outDir = DEFAULT_OUT }) {
  const runDir = path.join(outDir, runId);
  const normalizedDir = path.join(runDir, 'normalized');
  const rawDir = path.join(runDir, 'raw');

  if (!existsSync(runDir)) {
    throw new Error(`找不到运行目录 ${runDir}。请先执行 jira-fetch.mjs 完成拉取。`);
  }

  const expectedKeys = existsSync(rawDir)
    ? (await readdir(rawDir))
        .filter((name) => name.endsWith('.json'))
        .map((name) => name.replace(/\.json$/, ''))
        .sort()
    : [];

  const drafts = existsSync(normalizedDir)
    ? (await readdir(normalizedDir)).filter((name) => name.endsWith('.spec.md')).sort()
    : [];

  const results = [];
  for (const draft of drafts) {
    results.push(await checkFile(path.join(normalizedDir, draft)));
  }

  const checkedKeys = new Set(results.map((result) => result.label));
  const missingDrafts = expectedKeys.filter((key) => !checkedKeys.has(key));

  const output = {
    runId,
    checkedAt: new Date().toISOString(),
    rulesVersion: 'openspec 1.12.0',
    rulesEvaluated: RULES.length,
    results,
    missingDrafts,
    summary: summarize(results),
  };

  const target = path.join(runDir, RESULT_FILE);
  await mkdir(runDir, { recursive: true });
  await writeFile(target, `${JSON.stringify(output, null, 2)}\n`, 'utf8');

  return { output, target, runDir };
}

export function summarize(results) {
  const conclusions = { 合格: 0, 需修改: 0, 不合格: 0 };
  const ruleFailures = new Map();

  for (const result of results) {
    conclusions[result.conclusion] = (conclusions[result.conclusion] ?? 0) + 1;
    for (const item of result.allIssues) {
      ruleFailures.set(item.ruleId, (ruleFailures.get(item.ruleId) ?? 0) + 1);
    }
  }

  const total = {
    ERROR: 0,
    WARNING: 0,
    INFO: 0,
    total: 0,
  };
  for (const result of results) {
    const counts = countBySeverity(result.allIssues);
    total.ERROR += counts.ERROR;
    total.WARNING += counts.WARNING;
    total.INFO += counts.INFO;
    total.total += counts.total;
  }

  return {
    specsChecked: results.length,
    conclusions,
    issueTotals: total,
    ruleFailureRanking: [...ruleFailures.entries()]
      .map(([ruleId, count]) => ({ ruleId, count }))
      .sort((a, b) => b.count - a.count || a.ruleId.localeCompare(b.ruleId)),
  };
}

/** 终端可读输出。 */
export function formatText(result) {
  const lines = [];
  const counts = countBySeverity(result.allIssues);
  lines.push(`【${result.label}】结论：${result.conclusion}`);
  lines.push(
    `  规则 ${result.passedRuleCount}/${result.rulesEvaluated} 通过；` +
      `问题 ${counts.total} 个（ERROR ${counts.ERROR} / WARNING ${counts.WARNING} / INFO ${counts.INFO}）`,
  );
  lines.push(`  需求 ${result.requirements.length} 条`);

  for (const requirement of result.requirements) {
    lines.push(`  - ${requirement.name} → ${requirement.conclusion}（场景 ${requirement.scenarioCount} 个）`);
    for (const item of requirement.issues) {
      lines.push(`      [${item.severity}] ${item.ruleId} 第 ${item.line} 行：${item.summary}`);
    }
  }
  for (const item of result.specIssues) {
    lines.push(`  [${item.severity}] ${item.ruleId}${item.line ? ` 第 ${item.line} 行` : ''}：${item.summary}`);
  }
  return lines.join('\n');
}

export function formatRuleTable() {
  return RULES.map((rule) => `[${rule.severity.padEnd(7)}] ${rule.id}\n          ${rule.summary}`).join('\n');
}

async function main() {
  const logger = createLogger({});
  let args;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (error) {
    logger.error(`${error.message}\n用法：node scripts/rulecheck.mjs --run-id <id> | --spec <file> | --list-rules`);
    process.exitCode = 4;
    return;
  }

  if (args.listRules) {
    logger.out(`规则表（共 ${RULES.length} 条，对齐 openspec 1.12.0）：`);
    logger.out(formatRuleTable());
    return;
  }

  try {
    if (args.spec) {
      const result = await checkFile(args.spec);
      logger.out(args.format === 'text' ? formatText(result) : JSON.stringify(result, null, 2));
      return;
    }

    const { output, target } = await checkRun({ runId: args.runId, outDir: args.outDir ?? DEFAULT_OUT });
    logger.out(
      args.format === 'text'
        ? output.results.map(formatText).join('\n\n')
        : JSON.stringify(output, null, 2),
    );
    logger.info(`规则结果已写入 ${target}`);
    if (output.missingDrafts.length > 0) {
      logger.warn(`以下 issue 尚无归一化草稿，本次未校验：${output.missingDrafts.join(', ')}`);
    }
  } catch (error) {
    logger.error(error.message);
    process.exitCode = 5;
  }
}

if (process.argv[1] && process.argv[1].endsWith('rulecheck.mjs')) {
  await main();
}
