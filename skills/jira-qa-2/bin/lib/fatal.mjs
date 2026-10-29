/**
 * 统一异常收口：任何脚本异常都写 result.json、尽量在 Jira 评论，
 * 并以 exit 0 结束，避免平台把技能中断。
 *
 * 阶段一：写「执行异常」评论。
 * 阶段二：区分「不满足执行条件」与「执行异常」，都写评论。
 * 阶段三：只写本地 result，不写 Jira 评论。
 */

import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';

import { EXIT, loadCredentials, parseArgs, parseIssueKey, resolveWorkspace } from './env.mjs';
import { JiraClient } from './jira.mjs';
import { ensureDir, writeJson } from './fs-safe.mjs';
import { makeRunId, printResult, redact } from './result.mjs';
import { syncStatusComment } from './comment-sync.mjs';

const CODE_NAMES = {
  [EXIT.USAGE]: 'USAGE',
  [EXIT.AUTH]: 'JIRA_AUTH',
  [EXIT.NOT_FOUND]: 'JIRA_NOT_FOUND',
  [EXIT.NETWORK]: 'JIRA_NETWORK',
  [EXIT.VALIDATION]: 'VALIDATION',
  [EXIT.PRECONDITION]: 'PRECONDITION',
  [EXIT.REMOTE_WRITE]: 'JIRA_WRITE',
  [EXIT.INTERNAL]: 'INTERNAL',
};

const NOT_SATISFIED_CODES = new Set(['USAGE', 'PRECONDITION', 'SKIPPED']);

function normalizeStage(value) {
  const raw = String(value ?? '').trim();
  return { 1: 'risk-analysis', 2: 'design', 3: 'cases' }[raw] || raw;
}

function bestEffortContext(argv) {
  const args = parseArgs(argv);
  let key = '';
  try {
    key = parseIssueKey(args.key || args._[0] || '');
  } catch {
    key = '';
  }
  let workspace = process.cwd();
  try {
    workspace = resolveWorkspace(args.workspace);
  } catch {
    workspace = path.resolve(process.cwd());
  }
  let stage = args.stage || '';
  let deliverableVersion = '';
  if (args.spec && existsSync(args.spec)) {
    try {
      const spec = JSON.parse(readFileSync(args.spec, 'utf8'));
      key = key || String(spec.jiraKey || '');
      stage = stage || (spec.stage || 'risk-analysis');
      deliverableVersion = String(spec.deliverableVersion || '');
    } catch {
      /* ignore */
    }
  }
  const runId = args['run-id'] || makeRunId(key || 'unknown');
  return { args, key, workspace, runId, stage, deliverableVersion };
}

async function commentStageOne({ key, stage, message, errorCode }) {
  const creds = loadCredentials(process.env, { required: false });
  if (!key || !creds.baseUrl || !creds.token) return { commentId: '', warning: '缺少 Jira 凭据或 Key，未写评论' };
  try {
    const client = new JiraClient(creds);
    const body = `执行异常\n阶段：${stage || 'unknown'}\n原因：${message}\n标记：${errorCode}\n本次未生成门禁结论。`;
    const comments = await client.getComments(key);
    const existing = comments.find(
      (comment) => String(comment.body || '').includes('执行异常') && String(comment.body || '').includes('本次未生成门禁结论'),
    );
    if (existing) {
      await client.updateComment(key, existing.id, body);
      return { commentId: String(existing.id), warning: '' };
    }
    const created = await client.createComment(key, body);
    return { commentId: String(created.id), warning: '' };
  } catch (error) {
    return { commentId: '', warning: `执行异常评论写入失败：${error.message}` };
  }
}

async function commentStageTwo({ key, workspace, runId, message, errorCode }) {
  const creds = loadCredentials(process.env, { required: false });
  if (!key || !creds.baseUrl || !creds.token) return { commentId: '', warning: '缺少 Jira 凭据或 Key，未写评论' };
  try {
    const client = new JiraClient(creds);
    const notSatisfied = NOT_SATISFIED_CODES.has(errorCode);
    const body = notSatisfied
      ? `h3. QA-测试方案不满足执行条件\n*执行结论：不满足执行条件，本次未生成测试方案*\n原因：${message}\n标记：${errorCode}`
      : `h3. QA-测试方案执行异常\n*执行结论：失败，本次未生成测试方案*\n阶段：design\n原因：${message}\n标记：${errorCode}\n本次未修改阶段一需求准入门禁结论与评论。`;
    const out = await syncStatusComment({ client, key, body, runId, workspace });
    return { commentId: out.commentId, warning: '' };
  } catch (error) {
    return { commentId: '', warning: `阶段二评论写入失败：${error.message}` };
  }
}

async function handleFatalInner(error, { argv = process.argv.slice(2), stage = '', script = 'qa-gate', comment } = {}) {
  const message = error?.message || String(error);
  const numeric = error?.code;
  const errorCode = typeof error?.errorCode === 'string' ? error.errorCode : CODE_NAMES[numeric] || 'INTERNAL';
  process.stderr.write(`[${script}][ERR] ${message}\n`);

  let context;
  try {
    context = bestEffortContext(argv);
  } catch {
    context = {
      key: '',
      workspace: path.resolve(process.cwd()),
      runId: makeRunId('unknown'),
      stage,
      deliverableVersion: '',
    };
  }
  const rawStage = stage || context.stage || '';
  const finalStage = normalizeStage(rawStage) || 'risk-analysis';
  const shouldComment = comment === undefined
    ? finalStage === 'risk-analysis' || finalStage === 'design'
    : Boolean(comment);

  const warnings = [];
  let commentResult = { commentId: '', warning: '' };
  if (shouldComment && finalStage === 'risk-analysis') {
    commentResult = await commentStageOne({
      key: context.key,
      stage: finalStage,
      message,
      errorCode,
    }).catch((err) => ({ commentId: '', warning: `执行异常评论失败：${err.message}` }));
  } else if (shouldComment && finalStage === 'design') {
    commentResult = await commentStageTwo({
      key: context.key,
      workspace: context.workspace,
      runId: context.runId,
      message,
      errorCode,
    }).catch((err) => ({ commentId: '', warning: `阶段二评论失败：${err.message}` }));
  } else {
    warnings.push('子任务异常：按约定不写 Jira 评论，不影响阶段一门禁结论和评论');
  }
  if (commentResult.warning) warnings.push(commentResult.warning);

  const result = {
    schemaVersion: 1,
    stage: finalStage,
    ok: false,
    applicable: true,
    status: 'ERROR',
    jiraKey: context.key,
    conclusion: '',
    failedItemCount: 0,
    pendingItemCount: 0,
    deliverableVersion: context.deliverableVersion || '',
    htmlPath: '',
    attachmentId: '',
    commentId: commentResult.commentId,
    materialAttachmentIds: [],
    errorCode,
    error: message,
    reason: message,
    nextCommand: '',
    warnings,
  };

  try {
    const runDir = path.join(context.workspace, '.qa', 'runs', context.runId);
    await ensureDir(runDir);
    await writeJson(path.join(runDir, 'result.json'), redact(result));
  } catch (writeError) {
    warnings.push(`result.json 写入失败：${writeError.message}`);
  }

  printResult(result);
  process.exitCode = 0;
  return result;
}

/**
 * 对外包装：即使异常收口本身失败，也不让进程以非零码退出。
 */
export async function handleFatal(error, options = {}) {
  try {
    return await handleFatalInner(error, options);
  } catch (fatalError) {
    try {
      process.stderr.write(`[${options.script || 'qa-gate'}][ERR] 异常收口失败：${fatalError.message}\n`);
    } catch {
      /* ignore */
    }
    try {
      process.stdout.write(
        `QA_RESULT_JSON\n${JSON.stringify({
          schemaVersion: 1,
          stage: options.stage || 'risk-analysis',
          ok: false,
          applicable: true,
          status: 'ERROR',
          errorCode: 'INTERNAL',
          error: String(error?.message || error),
        })}\n`,
      );
    } catch {
      /* ignore */
    }
    process.exitCode = 0;
  }
}

// 最后一道保险：未捕获异常/未处理拒绝也不以非零码退出。
process.on('unhandledRejection', (reason) => {
  handleFatal(reason, { argv: process.argv.slice(2), script: 'unhandledRejection' });
});
process.on('uncaughtException', (error) => {
  handleFatal(error, { argv: process.argv.slice(2), script: 'uncaughtException' });
});
