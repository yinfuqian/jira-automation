/**
 * 机器可读运行结果：写 .qa/runs/<runId>/result.json，并在 stdout 末尾打印 QA_RESULT_JSON。
 */

import path from 'node:path';

import { writeJson } from './fs-safe.mjs';

export function makeRunId(key) {
  const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\..+$/, '').replace('T', '-');
  return `${key}-${stamp}`;
}

export function redact(value) {
  const text = JSON.stringify(value);
  return JSON.parse(
    text
      .replace(/("?(?:JIRA_PAT|JIRA_TOKEN|Authorization)"?\s*:\s*")[^"]+(")/gi, '$1***$2')
      .replace(/Bearer\s+[A-Za-z0-9._-]+/g, 'Bearer ***'),
  );
}

export async function writeResult(workspace, runId, result) {
  const file = path.join(workspace, '.qa', 'runs', runId, 'result.json');
  await writeJson(file, redact(result));
  return file;
}

export function printResult(result) {
  process.stdout.write(`QA_RESULT_JSON\n${JSON.stringify(redact(result))}\n`);
}

/**
 * 输入不符合门禁要求时的“跳过”结果：不算失败、不中断平台流程、不写 Jira 评论。
 * 退出码保持 0；平台据 applicable=false / status=SKIPPED 继续后续流程。
 */
export async function writeSkipResult(workspace, runId, { jiraKey, reason, errorCode, attachments = [], stage = 'risk-analysis', commentId = '', warnings = [] }) {
  const result = {
    schemaVersion: 1,
    stage,
    ok: true,
    applicable: false,
    status: 'SKIPPED',
    jiraKey,
    conclusion: '',
    failedItemCount: 0,
    deliverableVersion: '',
    htmlPath: '',
    attachmentId: '',
    commentId: String(commentId || ''),
    materialAttachmentIds: attachments.map((a) => String(a.id ?? a)),
    errorCode,
    reason,
    nextCommand: '',
    warnings,
  };
  const file = await writeResult(workspace, runId, result);
  printResult(result);
  return { result, file };
}
