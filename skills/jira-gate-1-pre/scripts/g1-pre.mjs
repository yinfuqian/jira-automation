#!/usr/bin/env node
/**
 * G1-Pre 准入检查辅助脚本。
 *
 * 用途：把「读单子 → 计算内容摘要 → 渲染评论 → 判重」这些容易写错的机械动作固化下来，
 * 让模型只负责 F01~F07 的业务判定，不负责拼 Jira Wiki 标记与哈希格式。
 *
 * 用法：
 *   node scripts/g1-pre.mjs precheck <KEY|URL> [--transition-id X] [--source-status X] [--target-status X]
 *   node scripts/g1-pre.mjs context  <KEY|URL> [--description-limit 20000] [--comments 10]
 *                                    [--transition-id X] [--source-status X] [--target-status X]
 *   node scripts/g1-pre.mjs hash     <KEY|URL> [--dir <附件目录>] [--transition-id X] [--source-status X] [--target-status X]
 *   node scripts/g1-pre.mjs history  <KEY|URL> [--file <result.json>]
 *   node scripts/g1-pre.mjs comment  --file <result.json> [--write] [--force] [--comment-id <ID>]
 *
 * 硬边界：本脚本只读单据、写评论；不提供流转状态、修改字段、上传或删除附件的能力。
 */

import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

import * as jira from "./jira.mjs";


/** 规则版本，与 references/g1-pre-rules.md 的版本保持一致。 */
const RULE_VERSION = "V1.0";

/** G1-Pre 适用的问题类型；未命中的类型由接入层过滤，本技能直接跳过。 */
const SUPPORTED_ISSUE_TYPES = [
  "客户用户故事",
  "故事",
  "项目概算评估",
  "任务",
  "项目实施阶段",
];

/**
 * 问题类型对应的预期 transition。仅用于记录与内容指纹，本技能绝不执行流转。
 * 接入层传入实际 transition 时以传入值为准。
 */
const TRANSITION_MAP = {
  客户用户故事: { action: "跟进需求", targetStatus: "需求澄清" },
  故事: { action: "跟进需求", targetStatus: "需求澄清" },
  项目概算评估: { action: "开始评估", targetStatus: "处理中" },
  任务: { action: "开始进行", targetStatus: "处理中" },
  项目实施阶段: { action: "开始评估", targetStatus: "处理中" },
};

/**
 * 客户用户故事 / 故事 进入「需求澄清」后，`需求设计 → 评审中` 属于产品设计完成后的内部流转，
 * 不再重复执行 G1-Pre。仅在接入层显式传入目标状态时据此跳过，不做任何猜测。
 */
const SKIP_TARGET_STATUSES = ["评审中"];

/** 阻断问题分类的中文名，必须与规则文档第 5 节完全一致。 */
const CATEGORY_LABELS = {
  background_content_mismatch: "背景与需求内容不一致",
  expectation_contradiction: "需求期望前后不一致",
  missing_key_information: "缺失关键信息",
  material_ambiguity: "关键表述有歧义",
  scope_or_ownership_unclear: "范围或归属不清",
  unverifiable_target: "目标不可验证",
  key_decision_unresolved: "关键决策未定",
  evidence_inconsistent_or_missing: "描述与证据不一致或缺失",
  other: "其他",
};

/** F01~F07 七项必判事实。 */
const REQUIRED_CHECKS = ["F01", "F02", "F03", "F04", "F05", "F06", "F07"];

/** 参与判定的自定义字段；内容指纹只覆盖这些字段，避免无关字段变化导致重复评论。 */
const PARTICIPATING_FIELD_NAMES = ["客户名称", "产品归属", "问题发现产品", "发现产品", "关联问题"];


const USAGE = `用法：
  node scripts/g1-pre.mjs precheck <KEY|URL> [--transition-id X] [--source-status X] [--target-status X]
  node scripts/g1-pre.mjs context  <KEY|URL> [--description-limit 20000] [--comments 10]
  node scripts/g1-pre.mjs hash     <KEY|URL> [--dir <附件目录>] [--transition-id X] [--source-status X] [--target-status X]
  node scripts/g1-pre.mjs history  <KEY|URL> [--file <result.json>]
  node scripts/g1-pre.mjs comment  --file <result.json> [--write] [--force] [--comment-id <ID>]`;


/** 解析命令行参数，支持 --key value 与位置参数。 */
function parseArgs(argv) {
  const positional = [];
  const options = {};
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (item.startsWith("--")) {
      const key = item.slice(2);
      const next = argv[index + 1];
      if (next === undefined || String(next).startsWith("--")) {
        options[key] = true;
      } else {
        options[key] = next;
        index += 1;
      }
    } else {
      positional.push(item);
    }
  }
  return { positional, options };
}


/** 递归按字典序排列对象键，保证同一内容每次序列化结果一致。 */
function sortKeys(value) {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (value && typeof value === "object") {
    const sorted = {};
    for (const key of Object.keys(value).sort()) sorted[key] = sortKeys(value[key]);
    return sorted;
  }
  return value;
}


/** 计算字符串的 sha256，统一带前缀便于识别算法。 */
function sha256(text) {
  return `sha256:${createHash("sha256").update(text, "utf8").digest("hex")}`;
}


/**
 * 解析本次检查的 transition 上下文：
 * 1. 接入层显式传入时以传入值为准；
 * 2. 未传入时按问题类型给出预期目标状态，并标记 provided=false；
 * 3. 源状态默认取单据当前状态。
 */
function resolveTransition(issue, options) {
  const mapped = TRANSITION_MAP[issue.issueType] || { action: "", targetStatus: "" };
  const provided = Boolean(options["transition-id"] || options["target-status"] || options["source-status"]);
  return {
    provided,
    id: options["transition-id"] ? String(options["transition-id"]) : "",
    name: options["transition-name"] ? String(options["transition-name"]) : mapped.action,
    sourceStatus: options["source-status"] ? String(options["source-status"]) : issue.status,
    targetStatus: options["target-status"] ? String(options["target-status"]) : mapped.targetStatus,
    expectedTargetStatus: mapped.targetStatus,
  };
}


/** 输出 JSON 到 stdout，统一两个空格缩进。 */
function print(payload) {
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
}


/** 类型预检：判断问题类型是否属于 G1-Pre 的五种上游事项，并排除不再重复检查的流转。 */
async function commandPrecheck(positional, options) {
  const target = positional[0];
  if (!target) throw new Error("缺少 Jira 链接或单号");
  const issue = await jira.getIssue(target, options);
  const issueType = String(issue.issueType || "").trim();
  const typeSupported = SUPPORTED_ISSUE_TYPES.includes(issueType);
  const transition = resolveTransition(issue, options);
  // 只在接入层显式传入目标状态时判断：需求设计 → 评审中 不属于 G1-Pre 的受理前置检查。
  const explicitTarget = options["target-status"] ? String(options["target-status"]).trim() : "";
  const skipByTarget = Boolean(
    typeSupported
    && ["客户用户故事", "故事"].includes(issueType)
    && SKIP_TARGET_STATUSES.includes(explicitTarget),
  );
  const supported = typeSupported && !skipByTarget;
  print({
    ok: true,
    key: issue.key,
    summary: issue.summary,
    issueType,
    expectedIssueTypes: SUPPORTED_ISSUE_TYPES,
    typeSupported,
    supported,
    status: issue.status,
    transition,
    skipReason: supported
      ? ""
      : (typeSupported
        ? `目标状态「${explicitTarget}」属于需求设计完成后的内部流转，不再重复执行 G1-Pre，按规则跳过。`
        : `问题类型「${issueType || "(空)"}」不属于 G1-Pre 适用的五类上游事项，按规则跳过。`),
  });
}


/** 读取判定所需的全部上下文，输出紧凑 JSON 供模型判定。 */
async function commandContext(positional, options) {
  const target = positional[0];
  if (!target) throw new Error("缺少 Jira 链接或单号");
  const issue = await jira.getIssue(target, options);
  const descriptionLimit = Number(options["description-limit"] || 20000);
  const commentLimit = Number(options.comments || 10);
  const description = descriptionLimit > 0 && issue.description.length > descriptionLimit
    ? `${issue.description.slice(0, descriptionLimit)}\n…（已截断，全文共 ${issue.description.length} 字）`
    : issue.description;
  const comments = issue.comments.slice(-commentLimit).map((comment) => ({
    id: comment.id,
    author: comment.author,
    authorName: comment.authorName,
    created: comment.created,
    body: comment.body.length > 1500 ? `${comment.body.slice(0, 1500)}…（截断）` : comment.body,
  }));
  print({
    ok: true,
    key: issue.key,
    url: issue.url,
    summary: issue.summary,
    issueType: issue.issueType,
    status: issue.status,
    priority: issue.priority,
    reporter: issue.reporter,
    reporterMention: issue.reporter && issue.reporter.name ? `[~${issue.reporter.name}]` : "",
    assignee: issue.assignee,
    creator: issue.creator,
    project: issue.project,
    labels: issue.labels,
    components: issue.components,
    created: issue.created,
    updated: issue.updated,
    customFields: issue.customFields,
    attachments: issue.attachments,
    description,
    descriptionLength: issue.description.length,
    commentCount: issue.comments.length,
    comments,
    transition: resolveTransition(issue, options),
  });
}


/** 计算内容指纹：覆盖标题、描述、附件、参与字段、问题类型与 transition。 */
async function commandHash(positional, options) {
  const target = positional[0];
  if (!target) throw new Error("缺少 Jira 链接或单号");
  const issue = await jira.getIssue(target, options);
  const transition = resolveTransition(issue, options);
  const attachmentDir = options.dir ? String(options.dir) : "";

  const attachments = issue.attachments.map((attachment) => {
    const base = {
      id: attachment.id,
      filename: attachment.filename,
      size: attachment.size,
      created: attachment.created,
    };
    if (!attachmentDir) {
      // 未下载附件时退化为元数据指纹；主流程应始终下载后带 --dir 计算。
      return { ...base, hash: `meta:${attachment.id}:${attachment.size}:${attachment.created}` };
    }
    const safeName = String(attachment.filename).replace(/[\\/:*?"<>|]/g, "_");
    const localPath = join(attachmentDir, safeName);
    if (!existsSync(localPath)) {
      throw new Error(`附件目录缺少文件：${localPath}；请先下载全部附件再计算内容指纹`);
    }
    return { ...base, hash: sha256(readFileSync(localPath)) };
  });

  const parts = sortKeys({
    ruleVersion: RULE_VERSION,
    issueKey: issue.key,
    issueType: issue.issueType,
    summary: issue.summary,
    description: issue.description,
    status: issue.status,
    dueDate: issue.dueDate,
    priority: issue.priority,
    labels: issue.labels,
    components: issue.components,
    customFields: Object.fromEntries(
      Object.entries(issue.customFields).filter(
        ([name, value]) => PARTICIPATING_FIELD_NAMES.includes(name) && String(value || "").trim(),
      ),
    ),
    transition: {
      id: transition.id,
      name: transition.name,
      sourceStatus: transition.sourceStatus,
      targetStatus: transition.targetStatus,
    },
    attachments,
  });
  print({
    ok: true,
    key: issue.key,
    ruleVersion: RULE_VERSION,
    contentHash: sha256(JSON.stringify(parts)),
    attachmentMode: attachmentDir ? "sha256" : "metadata",
    parts,
  });
}


/**
 * 识别一条 G1-Pre 评论。
 * 新版本使用 Jira Wiki 标题（`h3. (/) G1-Pre 准入通过` / `h3. (!) G1-Pre 准入未通过（N 项待补充）`）；
 * 旧版本使用 `G1-Pre 结论` + 指纹，保留识别能力只为兼容历史评论，新评论不再写入指纹。
 */
function parseGateComment(comment) {
  const body = String(comment.body || "").replace(/\r\n/g, "\n");
  const passMatch = /^h3\.\s*\(\/\)\s*G1-Pre\s*准入通过/m.test(body);
  const failMatch = body.match(/^h3\.\s*\(!\)\s*G1-Pre\s*准入未通过[（(](\d+)\s*项待补充[）)]/m);
  const legacyConclusion = body.match(/G1-Pre\s*结论[:：]\s*(通过|不通过)/);
  const legacyMark = /G1-Pre\s*指纹[:：]/i.test(body);
  if (!passMatch && !failMatch && !legacyConclusion && !legacyMark) return null;
  return {
    commentId: comment.id,
    author: comment.author,
    created: comment.created,
    conclusion: passMatch ? "通过" : (failMatch || legacyConclusion ? "不通过" : ""),
    blockingCount: failMatch ? Number(failMatch[1]) : 0,
    format: passMatch || failMatch ? "wiki" : "legacy",
    body,
  };
}


/**
 * 归一化评论正文，用于判重：
 * 1. 统一换行符；
 * 2. 去掉每行行尾空格；
 * 3. 折叠连续空行。
 */
function normalizeBody(text) {
  return String(text || "")
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .map((line) => line.replace(/[ \t]+$/, ""))
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}


/**
 * 查找历史 G1-Pre 评论并判断本轮评论是否与最新一条完全一致。
 *
 * 规则文档要求评论中不得出现规则版本或内容指纹，因此幂等改为比对「最新一条 G1-Pre 评论正文」：
 * 正文由结论、阻断项和规范建议决定，正文相同即无需重复评论。传入 --file 时按 result.json 渲染后比对。
 */
async function commandHistory(positional, options) {
  const target = positional[0];
  if (!target) throw new Error("缺少 Jira 链接或单号");
  const issue = await jira.getIssue(target, options);
  const entries = issue.comments.map(parseGateComment).filter(Boolean);
  const latest = entries.length ? entries[entries.length - 1] : null;

  let rendered = "";
  if (options.file) {
    const result = readResult(String(options.file));
    const mention = issue.reporter && issue.reporter.name ? `[~${issue.reporter.name}]` : "";
    rendered = renderComment(result, mention);
  }
  const duplicate = Boolean(
    latest
    && rendered
    && latest.format === "wiki"
    && normalizeBody(latest.body) === normalizeBody(rendered),
  );

  print({
    ok: true,
    key: issue.key,
    duplicate,
    matchedCommentId: duplicate ? latest.commentId : "",
    latest: latest
      ? {
        commentId: latest.commentId,
        author: latest.author,
        created: latest.created,
        conclusion: latest.conclusion,
        blockingCount: latest.blockingCount,
        format: latest.format,
      }
      : null,
    entries: entries.map((entry) => ({
      commentId: entry.commentId,
      author: entry.author,
      created: entry.created,
      conclusion: entry.conclusion,
      blockingCount: entry.blockingCount,
      format: entry.format,
    })),
  });
}


/**
 * 对模型产出的文本做 Jira wiki 安全处理：
 * 1. 阻断 `[~user]` 形式的意外提及；
 * 2. 阻断 `{{monospace}}` 被误渲染；
 * 3. 压缩行尾空白，避免评论过长。
 */
function safeText(value) {
  return String(value || "")
    .replace(/\[~/g, "[\u200B~")
    .replace(/\{\{/g, "{\u200B{")
    .replace(/[ \t]+\n/g, "\n")
    .trim();
}


/** 读取并校验 result.json，确保结论与检查项自洽。 */
function readResult(file) {
  const payload = JSON.parse(readFileSync(file, "utf8"));
  const conclusion = String(payload.conclusion || "").trim().toLowerCase();
  if (!["pass", "fail"].includes(conclusion)) {
    throw new Error("result.json 的 conclusion 只能是 pass 或 fail");
  }
  // contentHash / ruleVersion 只供接入层幂等与审计使用，不写入评论；存在时校验格式，缺失不阻断。
  const hash = String(payload.contentHash || "").trim();
  if (hash && !/^sha256:[0-9a-f]{64}$/i.test(hash)) {
    throw new Error("result.json 的 contentHash 格式非法：应为完整的 sha256:... 值（先执行 g1-pre.mjs hash）");
  }
  const checks = Array.isArray(payload.checks) ? payload.checks : [];
  const checkIds = checks.map((check) => String(check.id || "").toUpperCase());
  const missing = REQUIRED_CHECKS.filter((id) => !checkIds.includes(id));
  if (missing.length) {
    throw new Error(`checks 必须覆盖 F01~F07，缺少：${missing.join("、")}`);
  }
  const findings = Array.isArray(payload.findings) ? payload.findings : [];
  for (const finding of findings) {
    if (!CATEGORY_LABELS[finding.category]) {
      throw new Error(`findings[].category 非法：${finding.category}`);
    }
    if (!String(finding.currentProblem || "").trim() || !String(finding.suggestedSupplement || "").trim()) {
      throw new Error("每条 finding 都必须包含 currentProblem 与 suggestedSupplement");
    }
  }
  if (conclusion === "pass" && findings.length) {
    throw new Error("findings 非空时 conclusion 必须是 fail");
  }
  if (conclusion === "fail" && !findings.length) {
    throw new Error("conclusion 为 fail 时 findings 不能为空");
  }
  return { payload, conclusion, findings };
}


/**
 * 把规范建议渲染为 `* *标签：* 内容` 形式。
 * 支持结构化对象（label/field + suggestion/text）与纯字符串：字符串没有标签时退化为 `* 内容`。
 */
function renderAdvisoryLines(payload) {
  const items = [
    ...(Array.isArray(payload.advisoryFindings) ? payload.advisoryFindings : []),
    ...(Array.isArray(payload.fieldHints) ? payload.fieldHints : []),
  ];
  const lines = [];
  for (const item of items) {
    if (item === null || item === undefined) continue;
    if (typeof item === "object") {
      const label = safeText(item.label || item.field || "");
      const text = safeText(item.suggestion || item.text || "");
      if (!text) continue;
      lines.push(label ? `* *${label}：* ${text}` : `* ${text}`);
      continue;
    }
    const text = safeText(item);
    if (text) lines.push(`* ${text}`);
  }
  return lines;
}


/**
 * 生成符合规则文档第 8 节的 Jira Wiki 评论正文。
 * 评论中不得出现 rule_version、content_hash、sha256 或「G1-Pre 指纹」。
 */
function renderComment(result, mention) {
  const { payload, conclusion, findings } = result;
  const lines = [];

  if (conclusion === "pass") {
    lines.push("h3. (/) G1-Pre 准入通过");
    lines.push("*结论：* 当前原始诉求已满足准入要求，可进入下一状态。");
  } else {
    lines.push(`h3. (!) G1-Pre 准入未通过（${findings.length} 项待补充）`);
    const prefix = mention ? `${mention} ` : "";
    lines.push(`*报告人：* ${prefix}请补充后重检。`);
    findings.forEach((finding, index) => {
      lines.push("");
      lines.push(`h4. ${index + 1}. ${CATEGORY_LABELS[finding.category]}`);
      lines.push(`*当前问题：* ${safeText(finding.currentProblem)}`);
      lines.push(`*建议调整：* ${safeText(finding.suggestedSupplement)}`);
    });
  }

  const advisory = renderAdvisoryLines(payload);
  if (advisory.length) {
    lines.push("");
    lines.push("h4. 规范建议");
    lines.push(...advisory);
  }

  return `${lines.join("\n").replace(/\n{3,}/g, "\n\n").trim()}\n`;
}


/** 渲染评论；--write 时先判重再写入。 */
async function commandComment(positional, options) {
  const file = options.file;
  if (!file) throw new Error("缺少 --file <result.json>");
  const result = readResult(String(file));
  const issueKey = String(result.payload.issueKey || "").trim();
  if (!issueKey) throw new Error("result.json 缺少 issueKey");

  // 写评论时才需要读单子做判重；只预览时尽量少发请求。
  const issue = options.write ? await jira.getIssue(issueKey, options) : null;
  let mention = String(options.mention || result.payload.reporterMention || "");
  // 只有不通过评论才需要 @报告人；通过评论不必为了补 mention 再读一次单子。
  if (!mention && result.conclusion === "fail") {
    const source = issue || await jira.getIssue(issueKey, options);
    mention = source.reporter && source.reporter.name ? `[~${source.reporter.name}]` : "";
  }
  const body = renderComment(result, mention);
  const output = {
    ok: true,
    key: issueKey,
    conclusion: result.conclusion,
    mention,
    mentionMissing: result.conclusion === "fail" && !mention,
    body,
    written: false,
    skipped: false,
    commentId: "",
  };

  if (!options.write) {
    print(output);
    return;
  }

  // 评论中不写指纹，判重改为比对最新一条 G1-Pre 评论正文：正文相同说明结论与补充要求未变化。
  const entries = issue.comments.map(parseGateComment).filter(Boolean);
  const latest = entries.length ? entries[entries.length - 1] : null;
  const unchanged = Boolean(
    latest
    && latest.format === "wiki"
    && normalizeBody(latest.body) === normalizeBody(body),
  );
  if (unchanged && !options.force) {
    output.skipped = true;
    output.commentId = latest.commentId;
    output.duplicateOf = latest.commentId;
    print(output);
    return;
  }

  if (options["comment-id"]) {
    const updated = await jira.updateComment(issueKey, String(options["comment-id"]), body, options);
    output.commentId = updated && updated.id ? updated.id : String(options["comment-id"]);
  } else {
    const created = await jira.addComment(issueKey, body, options);
    output.commentId = created && created.id ? created.id : "";
  }
  output.written = true;
  print(output);
}


async function main() {
  const { positional, options } = parseArgs(process.argv.slice(2));
  const command = positional.shift();
  switch (command) {
    case "precheck":
      await commandPrecheck(positional, options);
      return;
    case "context":
      await commandContext(positional, options);
      return;
    case "hash":
      await commandHash(positional, options);
      return;
    case "history":
      await commandHistory(positional, options);
      return;
    case "comment":
      await commandComment(positional, options);
      return;
    case "selftest":
      print(await jira.selftest(options));
      return;
    default:
      throw new Error(`未知命令：${command || "(空)"}\n${USAGE}`);
  }
}


main().catch((error) => {
  process.stderr.write(`[jira-gate-1-pre] ${(error && error.message) || error}\n`);
  // 用 exitCode 而不是 process.exit，避免 undici 连接尚未释放时强退触发 libuv 断言。
  process.exitCode = 1;
});
