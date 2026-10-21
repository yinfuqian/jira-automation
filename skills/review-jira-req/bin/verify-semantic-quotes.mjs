import { existsSync, readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';

import { validateSemantic } from './lib/semantic-schema.mjs';

// 用法：
//   node verify-semantic-quotes.mjs <run-id> <KEY> [KEY...]
//       —— 引用必须逐字来自 raw/<KEY>.json 的 descriptionClean 或附件抽取文本
//   node verify-semantic-quotes.mjs <run-id> <KEY> --draft
//       —— 引用改验 normalized/<KEY>.spec.md（评审对象是改写草稿而非 JIRA 原文时用），
//          此模式不搜索附件文本
//
// 结构校验直接复用报告端的契约（lib/semantic-schema.mjs），
// 避免出现"自检通过、报告却静默跳过"这种两边规则不一致的情况。

const DEFAULT_WORK_DIR = path.join(process.cwd(), 'reports', '.work');

/**
 * 读取某个 issue 的附件抽取文本：attachment id → 文本。
 * 优先读 raw 清单里的 textPath（更精确）；旧数据没有清单时回退到目录扫描。
 */
export function loadAttachmentTexts({ runId, key, workDir = DEFAULT_WORK_DIR } = {}) {
  const runDir = path.join(workDir, runId);
  const texts = new Map();
  const rawPath = path.join(runDir, 'raw', `${key}.json`);

  if (existsSync(rawPath)) {
    try {
      const raw = JSON.parse(readFileSync(rawPath, 'utf8'));
      for (const attachment of raw?.attachments ?? []) {
        if (attachment?.status !== 'text' || !attachment?.textPath) continue;
        const textFile = path.join(runDir, attachment.textPath);
        if (existsSync(textFile)) {
          texts.set(String(attachment.id), readFileSync(textFile, 'utf8'));
        }
      }
    } catch {
      // raw 损坏时走目录回退，不在这里报错——结构校验会另行提示。
    }
  }

  if (texts.size === 0) {
    const attachDir = path.join(runDir, 'attachments', key);
    if (existsSync(attachDir)) {
      for (const name of readdirSync(attachDir).filter((item) => item.endsWith('.txt'))) {
        const id = name.split('-')[0];
        texts.set(id, readFileSync(path.join(attachDir, name), 'utf8'));
      }
    }
  }

  return texts;
}

/**
 * 校验语义结果的引用是否逐字来自允许的来源。
 * @returns {{ total: number, problems: Array<{dimension: string, quote: string, reason: string}> }}
 */
export function verifyQuotes({
  semantic,
  descriptionClean = '',
  attachmentTexts = new Map(),
  draftText = null,
} = {}) {
  const problems = [];
  let total = 0;

  const push = (dimension, quote, reason) => {
    problems.push({ dimension, quote, reason });
  };

  for (const dimension of semantic?.dimensions ?? []) {
    for (const evidence of dimension?.evidence ?? []) {
      total += 1;
      const quote = evidence?.quote;
      const label = dimension?.id ?? '(未知维度)';

      if (typeof quote !== 'string' || quote.trim() === '') {
        push(label, String(quote ?? ''), '引用为空');
        continue;
      }

      const source = typeof evidence?.source === 'string' ? evidence.source : null;

      // 草稿模式只验草稿本身，不搜索附件。
      if (draftText !== null) {
        if (source && source !== 'description') {
          push(label, quote, `草稿模式不支持附件来源：${source}`);
        } else if (!draftText.includes(quote)) {
          push(label, quote, '引用非逐字（草稿）');
        }
        continue;
      }

      if (source === 'description') {
        if (!descriptionClean.includes(quote)) push(label, quote, '引用非逐字（description）');
        continue;
      }

      if (source?.startsWith('attachment:')) {
        const id = source.slice('attachment:'.length);
        if (!attachmentTexts.has(id)) {
          push(label, quote, `引用的附件不存在或未抽取：${source}`);
        } else if (!attachmentTexts.get(id).includes(quote)) {
          push(label, quote, `引用非逐字（${source}）`);
        }
        continue;
      }

      if (source) {
        push(label, quote, `无效的 source：${source}`);
        continue;
      }

      // 未声明来源：先查描述，再查任一附件。
      const candidates = [descriptionClean, ...attachmentTexts.values()];
      if (!candidates.some((text) => text.includes(quote))) {
        push(label, quote, '引用非逐字（描述与附件文本中均未找到）');
      }
    }
  }

  return { total, problems };
}

function main() {
  const args = process.argv.slice(2);
  const byDraft = args.includes('--draft');
  const [runId, ...keys] = args.filter((arg) => arg !== '--draft');

  let bad = 0;
  let total = 0;

  for (const key of keys) {
    const sourcePath = byDraft
      ? path.join(DEFAULT_WORK_DIR, runId, 'normalized', `${key}.spec.md`)
      : path.join(DEFAULT_WORK_DIR, runId, 'raw', `${key}.json`);
    const source = byDraft
      ? readFileSync(sourcePath, 'utf8')
      : JSON.parse(readFileSync(sourcePath, 'utf8')).descriptionClean;
    const sem = JSON.parse(
      readFileSync(path.join(DEFAULT_WORK_DIR, runId, 'semantic', `${key}.json`), 'utf8'),
    );

    for (const issue of validateSemantic(sem)) {
      bad += 1;
      console.log(`结构不合规 ${key}: ${issue}`);
    }

    const result = verifyQuotes({
      semantic: sem,
      descriptionClean: byDraft ? '' : source,
      attachmentTexts: byDraft ? new Map() : loadAttachmentTexts({ runId, key }),
      draftText: byDraft ? source : null,
    });
    total += result.total;

    for (const problem of result.problems) {
      bad += 1;
      console.log(`引用非逐字 [${problem.dimension}] ${key}: ${problem.quote}（${problem.reason}）`);
    }

    const quoteCount = (sem.dimensions ?? []).flatMap((dimension) => dimension.evidence ?? []).length;
    console.log(
      `${key}: 引用 ${quoteCount} | 建议场景 ${(sem.suggestedScenarios ?? []).length} | ` +
        `非原子 ${sem.nonAtomic?.detected}`,
    );
    console.log(
      `    ${(sem.dimensions ?? []).map((dimension) => `${dimension.id}=${dimension.conclusion}`).join(' ')}`,
    );
  }

  console.log(
    `\n校验（引用来源：${byDraft ? '改写草稿' : 'JIRA 描述与附件文本'}）：` +
      `引用逐字 ${total} 条，问题 ${bad} 个`,
  );
  process.exit(bad === 0 ? 0 : 1);
}

const isMain = process.argv[1] && process.argv[1].endsWith('verify-semantic-quotes.mjs');
if (isMain) main();
