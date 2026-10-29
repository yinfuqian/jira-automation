#!/usr/bin/env node
/**
 * 同步 Jira 评论：按“标题 + 材料版本行”定位；存在则编辑，否则新建。
 * 阶段一/阶段二共用；标题不同，互不覆盖。
 */

import { EXIT, QaError, boolArg, checkNode, loadCredentials, log, parseArgs, parseIssueKey, resolveWorkspace } from './lib/env.mjs';
import { JiraClient } from './lib/jira.mjs';
import { readText } from './lib/fs-safe.mjs';
import { findExistingComment, parseCommentBody, syncComment } from './lib/comment-sync.mjs';
import { handleFatal } from './lib/fatal.mjs';

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const key = parseIssueKey(args.key || args._[0]);
  const workspace = resolveWorkspace(args.workspace);
  const bodyFile = args.body;
  if (!bodyFile) {
    process.stderr.write('[jira-comment][ERR] 缺少 --body <wiki 文件>。\n');
    throw new QaError('参数或配置错误', EXIT.USAGE);
  }
  const body = await readText(bodyFile);
  const mode = args.mode || 'auto';
  const dryRun = boolArg(args, 'dry-run') || boolArg(args, 'no-comment');
  const runId = args['run-id'] || 'manual';
  const creds = loadCredentials(process.env, { required: !dryRun });

  if (dryRun) {
    const comments = creds.baseUrl && creds.token ? await new JiraClient(creds).getComments(key) : [];
    const existing = findExistingComment(comments, body);
    const action = existing && mode !== 'new' ? 'edit' : 'create';
    log('jira-comment', `dry-run ${action} title=${parseCommentBody(body).title}`);
    process.stdout.write(`ACTION=${action}\nDRY_RUN=1\n`);
    return;
  }

  const client = new JiraClient(creds);
  const out = await syncComment({
    client,
    key,
    body,
    mode,
    editId: args['edit-id'] || '',
    runId,
    workspace,
  });
  log('jira-comment', `${out.action} commentId=${out.commentId}`);
  process.stdout.write(`ACTION=${out.action}\nCOMMENT_ID=${out.commentId}\n`);
}

main().catch((error) =>
  handleFatal(error, {
    argv: process.argv.slice(2),
    stage: parseArgs(process.argv.slice(2)).stage || '',
    script: 'jira-comment',
  }),
);
