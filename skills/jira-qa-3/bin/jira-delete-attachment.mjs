#!/usr/bin/env node
/**
 * 删除单个 Jira 附件。
 * 默认只允许删除阶段交付物族（需求准入门禁清单 / 测试方案 / 测试用例）；
 * 其他附件必须显式 --force。
 */

import { EXIT, QaError, boolArg, checkNode, loadCredentials, log, parseArgs, parseIssueKey } from './lib/env.mjs';
import { JiraClient, attachmentList } from './lib/jira.mjs';
import { handleFatal } from './lib/fatal.mjs';

const DELIVERABLE = /-((需求准入门禁清单)|(测试方案)|(测试用例))-v\d+\.\d+\.(html|md|xmind)$/i;

async function main() {
  checkNode();
  const args = parseArgs(process.argv.slice(2));
  const key = parseIssueKey(args.key || args._[0]);
  const id = String(args.id || args._[1] || '');
  if (!id) {
    process.stderr.write('[jira-delete][ERR] 缺少 --id <附件ID>。\n');
    throw new QaError('参数或配置错误', EXIT.USAGE);
  }
  const dryRun = boolArg(args, 'dry-run');
  const client = new JiraClient(loadCredentials(process.env, { required: !dryRun }));
  let target = null;
  if (process.env.JIRA_BASE_URL) {
    const issue = await client.getIssue(key, { comments: false });
    target = attachmentList(issue).find((a) => a.id === id) || null;
    if (!target) {
      process.stderr.write(`[jira-delete][ERR] ${key} 下找不到附件 ${id}。\n`);
      throw new QaError('资源不存在', EXIT.NOT_FOUND);
    }
    if (!DELIVERABLE.test(target.filename) && !boolArg(args, 'force')) {
      process.stderr.write(
        `[jira-delete][ERR] 附件 ${id}（${target.filename}）不属于阶段交付物；如确需删除请加 --force。\n`,
      );
      throw new QaError('前置条件不满足', EXIT.PRECONDITION);
    }
    if (!boolArg(args, 'yes') && !boolArg(args, 'force')) {
      process.stderr.write('[jira-delete][ERR] 需要 --yes 确认删除。\n');
      throw new QaError('参数或配置错误', EXIT.USAGE);
    }
  }
  if (dryRun) {
    log('jira-delete', `dry-run delete ${id} ${target ? target.filename : ''}`);
    process.stdout.write('DRY_RUN=1\n');
    return;
  }
  await client.deleteAttachment(id);
  log('jira-delete', `deleted ${id} ${target ? target.filename : ''}`);
  process.stdout.write(`DELETED=${id}\n`);
}

main().catch((error) =>
  handleFatal(error, { argv: process.argv.slice(2), stage: "", script: "jira-delete-attachment" }),
);
