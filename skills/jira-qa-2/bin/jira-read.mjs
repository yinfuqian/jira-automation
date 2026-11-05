#!/usr/bin/env node
/**
 * 读取 Jira issue、附件元数据与评论，落盘 .qa/runs/<runId>/issue.json。
 */

import path from 'node:path';

import { EXIT, QaError, checkNode, loadCredentials, log, parseArgs, parseIssueKey, resolveWorkspace } from './lib/env.mjs';
import { JiraClient, attachmentList, isSupportedProduct, resolveProduct } from './lib/jira.mjs';
import { ensureDir, writeJson } from './lib/fs-safe.mjs';
import { makeRunId } from './lib/result.mjs';
import { handleFatal } from './lib/fatal.mjs';

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const key = parseIssueKey(args.key || args._[0]);
  const workspace = resolveWorkspace(args.workspace);
  const runId = args['run-id'] || makeRunId(key);
  const runDir = path.join(workspace, '.qa', 'runs', runId);
  await ensureDir(runDir);

  const client = new JiraClient(loadCredentials());
  // 读取全量字段并展开 names，才能按中文名解析「产品归属」等自定义字段。
  const issue = await client.getIssue(key, { comments: Number(args.comments ?? 0) > 0, allFields: true });
  const comments = Number(args.comments ?? 0) > 0 ? await client.getComments(key) : [];

  const product = resolveProduct(issue);
  const productSupported = isSupportedProduct(product.products);
  const productText = String(product.value || '').replace(/[\r\n]+/g, ',');

  const summary = {
    key,
    runId,
    summary: issue?.fields?.summary || '',
    status: issue?.fields?.status?.name || '',
    issueType: issue?.fields?.issuetype?.name || '',
    product: productText,
    productField: product.field,
    products: product.products,
    productSupported,
    components: (issue?.fields?.components || []).map((c) => c.name),
    labels: issue?.fields?.labels || [],
    updated: issue?.fields?.updated || '',
    attachments: attachmentList(issue),
    commentCount: comments.length,
    comments: comments.slice(-Number(args.comments ?? 0)).map((c) => ({
      id: String(c.id),
      author: c.author?.displayName || '',
      created: c.created,
      body: c.body || '',
    })),
  };

  await writeJson(path.join(runDir, 'issue.json'), issue);
  await writeJson(path.join(runDir, 'issue-summary.json'), summary);

  log(
    'jira-read',
    `key=${key} status=${summary.status} product=${productText || '(空)'} productSupported=${productSupported} attachments=${summary.attachments.length} comments=${summary.commentCount}`,
  );
  log('jira-read', `runId=${runId}`);
  log('jira-read', `issue=${path.join(runDir, 'issue.json')}`);
  if (args.json) process.stdout.write(`${JSON.stringify({ ok: true, runId, ...summary })}\n`);
  else {
    process.stdout.write(
      `RUN_ID=${runId}\nKEY=${key}\nSTATUS=${summary.status}\nPRODUCT=${productText}\nPRODUCT_SUPPORTED=${productSupported}\n` +
        `ATTACHMENTS=${summary.attachments.length}\n` +
        `RUN_DIR=${runDir}\n`,
    );
  }
}

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: parseArgs(process.argv.slice(2)).stage || '', script: "jira-read" }),
);
