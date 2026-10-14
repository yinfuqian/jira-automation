#!/usr/bin/env node
/**
 * jira-cli.mjs —— 缺陷单门禁技能的命令行入口（服务器容器 / codex exec 使用）。
 *
 * 用法：
 *   node scripts/jira-cli.mjs selftest                        # 令牌与连通性自检
 *   node scripts/jira-cli.mjs get <KEY|URL>                   # 输出紧凑 Markdown 摘要（含评论）
 *   node scripts/jira-cli.mjs json <KEY|URL>                  # 输出规范化 JSON
 *   node scripts/jira-cli.mjs selfcheck <KEY|URL>             # 判断评论区是否已有自查结果
 *   node scripts/jira-cli.mjs log-exemption <KEY|URL>         # 判断效果类缺陷是否明确声明豁免日志/复现
 *   node scripts/jira-cli.mjs scope <KEY|URL>                 # 提取产品、模块、版本与可选分支
 *   node scripts/jira-cli.mjs attachments <KEY|URL> <目录>     # 下载缺陷单附件
 *   node scripts/jira-cli.mjs comment <KEY|URL> --file <文件>  # 新增评论
 *   node scripts/jira-cli.mjs comment-update <KEY|URL> <评论ID> --file <文件>
 *   node scripts/jira-cli.mjs attach <KEY|URL> --file <文件>   # 上传附件（HTML 报告）
 *   node scripts/jira-cli.mjs gate-get <KEY|URL>              # 读取门禁字段
 *   node scripts/jira-cli.mjs gate-set <KEY|URL> --file <JSON> # 写入门禁字段
 *   node scripts/jira-cli.mjs transitions <KEY|URL>           # 列出可用流转
 *   node scripts/jira-cli.mjs transition <KEY|URL> --name <流转名> # 打回时自动恢复原经办人
 *
 * 令牌来源：--token 参数、JIRA_TOKEN 环境变量，或 jira.mjs 默认查找的令牌文件。
 */

import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import * as jira from "./jira.mjs";


const TOKEN_FILE_NAMES = [".jira-token", ".jira-token.txt", "jira-token.txt"];


const USAGE = `用法：
  node scripts/jira-cli.mjs selftest
  node scripts/jira-cli.mjs get <KEY|URL>
  node scripts/jira-cli.mjs json <KEY|URL>
  node scripts/jira-cli.mjs selfcheck <KEY|URL>
  node scripts/jira-cli.mjs log-exemption <KEY|URL>
  node scripts/jira-cli.mjs scope <KEY|URL>
  node scripts/jira-cli.mjs attachments <KEY|URL> <目录>
  node scripts/jira-cli.mjs comment <KEY|URL> --file <文件> [--body <文本>]
  node scripts/jira-cli.mjs comment-update <KEY|URL> <评论ID> --file <文件>
  node scripts/jira-cli.mjs attach <KEY|URL> --file <文件> [--filename <文件名>]
  node scripts/jira-cli.mjs gate-get <KEY|URL>
  node scripts/jira-cli.mjs gate-set <KEY|URL> --file <JSON文件>
  node scripts/jira-cli.mjs transitions <KEY|URL>
  node scripts/jira-cli.mjs transition <KEY|URL> --name <流转名>  # 打回时自动恢复原经办人`;


/** 解析命令行参数，支持 --key value 形式的选项与位置参数 */
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


/** 在技能目录及其上一级目录查找令牌文件（跨平台路径拼接） */
function readTokenFile() {
  const skillDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
  const candidates = [];
  for (const name of TOKEN_FILE_NAMES) {
    candidates.push(resolve(skillDir, name));
    candidates.push(resolve(skillDir, "..", name));
  }

  for (const file of candidates) {
    if (!existsSync(file)) continue;
    const text = readFileSync(file, "utf8").trim();
    if (!text) continue;
    if (text.startsWith("{")) {
      try {
        const config = JSON.parse(text);
        if (config.token) return { token: String(config.token).trim(), baseUrl: config.baseUrl };
      } catch (error) {
        continue;
      }
      continue;
    }
    const firstLine = text.split(/\r?\n/).find((line) => line.trim()) || "";
    const token = firstLine.replace(/^JIRA_PAT\s*=\s*/i, "").trim();
    if (token) return { token };
  }
  return null;
}


/** 组装 jira.mjs 需要的调用选项，命令行优先于环境变量与令牌文件 */
function buildOptions(options) {
  if (String(process.env.SKILLRUN_ENV_LOCKED || "").trim() === "1") {
    // 锁定模式：地址与令牌只认服务端注入的环境变量，不回落令牌文件，也不接受命令行改地址。
    const envToken = String(process.env.JIRA_TOKEN || "").trim();
    const envBaseUrl = String(process.env.JIRA_BASE_URL || "").trim().replace(/\/+$/, "");
    if (options.token && String(options.token).trim() !== envToken) {
      throw new Error("运行环境已锁定：不允许用 --token 覆盖服务端注入的 JIRA_TOKEN。");
    }
    if (options["base-url"] && String(options["base-url"]).trim().replace(/\/+$/, "") !== envBaseUrl) {
      throw new Error("运行环境已锁定：只允许访问 JIRA_BASE_URL 配置的站点 " + envBaseUrl + "。");
    }
    return {};
  }
  const result = {};
  const fromFile = readTokenFile() || {};
  const token = options.token || process.env.JIRA_TOKEN || fromFile.token;
  const baseUrl = options["base-url"] || process.env.JIRA_BASE_URL || fromFile.baseUrl;
  if (token) result.token = String(token);
  if (baseUrl) result.baseUrl = String(baseUrl);
  return result;
}


/** 读取评论或结果正文：优先 --file 文件内容，否则使用 --body 文本 */
function readBody(options) {
  if (typeof options.file === "string" && options.file) {
    return readFileSync(options.file, "utf8");
  }
  if (typeof options.body === "string") {
    return options.body;
  }
  throw new Error("缺少正文：请使用 --file <文件> 或 --body <文本> 传入");
}


/** 校验位置参数数量，缺失时给出明确的使用提示 */
function requireArgs(positional, count, usage) {
  if (positional.length < count) {
    throw new Error(`参数不足：${usage}`);
  }
}


async function main() {
  const { positional, options } = parseArgs(process.argv.slice(2));
  const command = positional.shift();
  if (!command || command === "help" || options.help) {
    console.log(USAGE);
    return;
  }

  const callOptions = buildOptions(options);
  switch (command) {
    case "selftest": {
      const result = await jira.selftest(callOptions);
      if (!result.ok && (result.steps || []).some((item) => item.step === "访问 Jira" && item.ok)) {
        result.hint = "站点可达即可继续用 get <KEY|URL> 验证取数；/myself 401 通常只是该令牌无账户接口权限。";
      }
      console.log(JSON.stringify(result, null, 2));
      return;
    }
    case "get": {
      requireArgs(positional, 1, "get <KEY|URL>");
      const issue = await jira.getIssue(positional[0], callOptions);
      console.log(jira.renderIssueMarkdown(issue));
      return;
    }
    case "json": {
      requireArgs(positional, 1, "json <KEY|URL>");
      const issue = await jira.getIssue(positional[0], callOptions);
      console.log(JSON.stringify(issue, null, 2));
      return;
    }
    case "selfcheck": {
      requireArgs(positional, 1, "selfcheck <KEY|URL>");
      const issue = await jira.getIssue(positional[0], callOptions);
      console.log(JSON.stringify(jira.findSelfCheckComments(issue), null, 2));
      return;
    }
    case "log-exemption": {
      requireArgs(positional, 1, "log-exemption <KEY|URL>");
      const issue = await jira.getIssue(positional[0], callOptions);
      console.log(JSON.stringify(jira.findLogRequirementExemption(issue), null, 2));
      return;
    }
    case "scope": {
      requireArgs(positional, 1, "scope <KEY|URL>");
      const issue = await jira.getIssue(positional[0], callOptions);
      console.log(JSON.stringify(jira.extractFaultAnalysisScope(issue), null, 2));
      return;
    }
    case "attachments": {
      requireArgs(positional, 2, "attachments <KEY|URL> <目录>");
      const saved = await jira.downloadAttachments(positional[0], positional[1], callOptions);
      console.log(JSON.stringify(saved, null, 2));
      return;
    }
    case "comment": {
      requireArgs(positional, 1, "comment <KEY|URL> --file <文件>");
      const body = readBody(options);
      const result = await jira.addComment(positional[0], body, callOptions);
      console.log(JSON.stringify({ ok: true, commentId: result && result.id }, null, 2));
      return;
    }
    case "comment-update": {
      requireArgs(positional, 2, "comment-update <KEY|URL> <评论ID> --file <文件>");
      const body = readBody(options);
      const result = await jira.updateComment(positional[0], positional[1], body, callOptions);
      console.log(JSON.stringify({ ok: true, commentId: result && result.id }, null, 2));
      return;
    }
    case "attach": {
      requireArgs(positional, 1, "attach <KEY|URL> --file <文件>");
      if (typeof options.file !== "string" || !options.file) {
        throw new Error("缺少附件：请使用 --file <文件>");
      }
      const result = await jira.addAttachment(positional[0], options.file, {
        ...callOptions,
        filename: typeof options.filename === "string" ? options.filename : undefined,
      });
      const uploaded = Array.isArray(result) ? result[0] : result;
      console.log(JSON.stringify({ ok: true, filename: uploaded && uploaded.filename, id: uploaded && uploaded.id }, null, 2));
      return;
    }
    case "gate-get": {
      requireArgs(positional, 1, "gate-get <KEY|URL>");
      console.log(JSON.stringify(await jira.getGateResult(positional[0], callOptions), null, 2));
      return;
    }
    case "gate-set": {
      requireArgs(positional, 1, "gate-set <KEY|URL> --file <JSON文件>");
      const payload = JSON.parse(readBody(options));
      console.log(JSON.stringify(await jira.setGateResult(positional[0], payload, callOptions), null, 2));
      return;
    }
    case "transitions": {
      requireArgs(positional, 1, "transitions <KEY|URL>");
      console.log(JSON.stringify(await jira.listTransitions(positional[0], callOptions), null, 2));
      return;
    }
    case "transition": {
      requireArgs(positional, 1, "transition <KEY|URL> --name <流转名>");
      const name = typeof options.name === "string" ? options.name : options.to;
      console.log(JSON.stringify(await jira.transitionIssue(positional[0], name, callOptions), null, 2));
      return;
    }
    default:
      throw new Error(`未知命令：${command}\n${USAGE}`);
  }
}


main().catch((error) => {
  console.error(String((error && error.message) || error));
  process.exit(1);
});
