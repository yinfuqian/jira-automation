/**
 * 统一异常收口：任何脚本异常都写 result.json、尽量在 Jira 评论「执行异常」，
 * 并以 exit 0 结束，避免平台把技能中断。失败事实通过 status=ERROR / ok=false 传递。
 */

import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';

import { EXIT, loadCredentials, parseArgs, parseIssueKey, resolveWorkspace } from './env.mjs';
import { JiraClient } from './jira.mjs';
import { ensureDir, writeJson } from './fs-safe.mjs';
import { makeRunId, printResult, redact } from './result.mjs';

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
      stage = stage || 'risk-analysis';
      deliverableVersion = String(spec.deliverableVersion || '');
    } catch {
      /* ignore */
    }
  }
  const runId = args['run-id'] || makeRunId(key || 'unknown');
  return { args, key, workspace, runId, stage, deliverableVersion };
}

async function commentException({ key, stage, message, errorCode }) {
  const creds = loadCredentials(process.env, { required: false });
  if (!key || !creds.baseUrl || !creds.token) return { commentId: '', warning: '缺少 Jira 凭据或 Key，未写评论' };
  try {
    const client = new JiraClient(creds);
    const body = `执行异常\n阶段：${stage || 'unknown'}\n原因：${message}\n标记：${errorCode}\n本次未生成门禁结论。`;
    const comments = await client.getComments(key);
    const existing = comments.find(
      (c) => String(c.body || '').includes('执行异常') && String(c.body || '').includes('本次未生成门禁结论'),
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
  const warnings = [];
  const shouldComment = comment === undefined ? Boolean(rawStage) && finalStage === 'risk-analysis' : Boolean(comment);

  let commentResult = { commentId: '', warning: '' };
  if (shouldComment) {
    commentResult = await commentException({
      key: context.key,
      stage: finalStage,
      message,
      errorCode,
    }).catch((err) => ({ commentId: '', warning: `执行异常评论失败：${err.message}` }));
    if (commentResult.warning) warnings.push(commentResult.warning);
  } else {
    warnings.push('子任务异常：按约定不写 Jira 评论，不影响阶段一门禁结论和评论');
  }

  const result = {
    schemaVersion: 1,
    stage: finalStage,
    ok: false,
    applicable: true,
    status: 'ERROR',
    jiraKey: context.key,
    conclusion: '',
    failedItemCount: 0,
    deliverableVersion: context.deliverableVersion || '',
    htmlPath: '',
    attachmentId: '',
    commentId: commentResult.commentId,
    materialAttachmentIds: [],
    errorCode,
    error: message,
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
