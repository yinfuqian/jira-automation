#!/usr/bin/env node
/**
 * 从 review-jira-req 生成的评审报告 Markdown 中提取规则结论。
 *
 * 用法：
 *   node scripts/report-conclusion.mjs <报告md路径> [KEY]
 *
 * 输出只有三个取值之一：不合格 / 需修改 / 合格，用于拼进 Jira 评论末尾的「评审结论：X」。
 * 报告里的这个取值来自规则校验（有 ERROR → 不合格；无 ERROR 但有 WARNING → 需修改；其余 → 合格），
 * 语义四维结论不并入该取值，因此只取每个 issue 小节里的「- 结论：**X**」那一行。
 *
 * 提取不到、取值非法或无法判断是哪个 issue 时，打到 stderr 并以退出码 1 结束，
 * 由调用方如实说明，而不是猜一个结论写进评论。
 */

import { readFileSync } from "node:fs";

// 允许写进评论的三个取值，与报告脚本的结论档位一致。
const ALLOWED = ["不合格", "需修改", "合格"];

/** 取出指定 issue 的小节；未指定 KEY 时返回整篇正文。 */
function sectionOf(text, key) {
  if (!key) return text;
  const lines = text.split(/\r?\n/);
  const start = lines.findIndex((line) => new RegExp(`^###\\s+${key}(\\s|$)`).test(line));
  if (start === -1) return "";
  // 截到下一个同级或更高级标题为止，避免把别的 issue 的结论算进来。
  let end = lines.length;
  for (let index = start + 1; index < lines.length; index += 1) {
    if (/^#{1,3}\s+/.test(lines[index])) {
      end = index;
      break;
    }
  }
  return lines.slice(start, end).join("\n");
}

/** 从小节里抽出「- 结论：**X**」的取值。 */
function conclusionOf(text) {
  const matches = [...text.matchAll(/^-\s*结论：\*\*(.+?)\*\*/gm)].map((m) => m[1].trim());
  if (!matches.length) return { ok: false, reason: "报告里没有「- 结论：**X**」这一行" };
  if (matches.length > 1) {
    return { ok: false, reason: `匹配到 ${matches.length} 条结论，请用 KEY 参数指定是哪个需求` };
  }
  const value = matches[0];
  if (!ALLOWED.includes(value)) {
    return { ok: false, reason: `结论取值非法：${value}（只允许 ${ALLOWED.join(" / ")}）` };
  }
  return { ok: true, value };
}

function main() {
  const [reportPath, key] = process.argv.slice(2);
  if (!reportPath) {
    throw new Error("用法：node scripts/report-conclusion.mjs <报告md路径> [KEY]");
  }
  const text = readFileSync(reportPath, "utf8");
  const section = sectionOf(text, key ? String(key).trim().toUpperCase() : "");
  if (!section) {
    throw new Error(`报告里找不到 ${key} 的小节（期望形如「### ${key} 标题」）`);
  }
  const conclusion = conclusionOf(section);
  if (!conclusion.ok) {
    throw new Error(conclusion.reason);
  }
  // 只输出结论本身，方便调用方直接拼进评论。
  process.stdout.write(`${conclusion.value}\n`);
}

try {
  main();
} catch (error) {
  process.stderr.write(`[提取评审结论失败] ${(error && error.message) || error}\n`);
  process.exitCode = 1;
}
