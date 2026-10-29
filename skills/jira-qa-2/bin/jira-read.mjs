#!/usr/bin/env node
/**
 * 读取 Jira issue、附件元数据与评论，落盘 .qa/runs/<runId>/issue.json。
 */

import path from 'node:path';

import { EXIT, QaError, checkNode, loadCredentials, log, parseArgs, parseIssueKey, resolveWorkspace } from './lib/env.mjs';
import { JiraClient, attachmentList } from './lib/jira.mjs';
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
  const issue = await client.getIssue(key, { comments: Number(args.comments ?? 0) > 0 });
  const comments = Number(args.comments ?? 0) > 0 ? await client.getComments(key) : [];

  const summary = {
    key,
    runId,
    summary: issue?.fields?.summary || '',
    status: issue?.fields?.status?.name || '',
    issueType: issue?.fields?.issuetype?.name || '',
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

  log('jira-read', `key=${key} status=${summary.status} attachments=${summary.attachments.length} comments=${summary.commentCount}`);
  log('jira-read', `runId=${runId}`);
  log('jira-read', `issue=${path.join(runDir, 'issue.json')}`);
  if (args.json) process.stdout.write(`${JSON.stringify({ ok: true, runId, ...summary })}\n`);
  else {
    process.stdout.write(
      `RUN_ID=${runId}\nKEY=${key}\nSTATUS=${summary.status}\nATTACHMENTS=${summary.attachments.length}\n` +
        `RUN_DIR=${runDir}\n`,
    );
  }
}

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: parseArgs(process.argv.slice(2)).stage || '', script: "jira-read" }),
);
