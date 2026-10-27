#!/usr/bin/env node
/**
 * 前置检查：Node 版本、凭据、Issue Key、工作区。
 * 不发起网络请求。
 */

import { checkNode, loadCredentials, parseArgs, parseIssueKey, resolveWorkspace, log, EXIT, QaError } from './lib/env.mjs';
import { handleFatal } from './lib/fatal.mjs';

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const key = parseIssueKey(args.key || args._[0]);
  const workspace = resolveWorkspace(args.workspace);
  const creds = loadCredentials();
  const scope = args.scope || process.env.QA_SCOPE || '';
  const out = {
    ok: true,
    node: process.versions.node,
    jiraKey: key,
    workspace,
    scope,
    jiraBaseUrl: creds.baseUrl,
    stage: args.stage || '1',
  };
  if (args.json) process.stdout.write(`${JSON.stringify(out)}\n`);
  else {
    log('preflight', `key=${key}`);
    log('preflight', `workspace=${workspace}`);
    log('preflight', `jira=${creds.baseUrl}`);
    log('preflight', 'OK');
  }
}

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: "", script: "preflight" }),
);
