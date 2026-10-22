#!/usr/bin/env node
/**
 * Jira 技能命令行入口（只读预演版）。
 *
 * 用途：在没有 node_repl 等桌面工具的运行环境（服务器容器、codex exec）中，
 * 直接通过 shell 调用 jira.mjs 的导出能力完成取数、下载附件与解析建议流转人。
 *
 * 只读边界：本入口只提供读命令，不提供任何写 Jira 的命令
 * （新增/修改评论、写门禁字段、上传附件、状态流转一概没有）。
 * 评论草稿与评审报告由调用方写进工作区，最后由 pack-deliverables.mjs 打成压缩包交付。
 *
 * 用法：
 *   node scripts/jira-cli.mjs selftest                       # 令牌与连通性自检
 *   node scripts/jira-cli.mjs precheck <KEY|URL>             # 单据类型预检（只处理「故事」）
 *   node scripts/jira-cli.mjs get <KEY|URL>                  # 输出紧凑 Markdown 摘要
 *   node scripts/jira-cli.mjs json <KEY|URL>                 # 输出规范化 JSON
 *   node scripts/jira-cli.mjs attachments <KEY|URL> <目录>    # 下载附件
 *   node scripts/jira-cli.mjs reviews <KEY|URL>              # 查找历史复审评论
 *   node scripts/jira-cli.mjs gate-get <KEY|URL>             # 读取门禁字段（只读，用于预演对比）
 *   node scripts/jira-cli.mjs transitions <KEY|URL>          # 列出当前可用的工作流流转
 *   node scripts/jira-cli.mjs flow-owner <KEY|URL>           # 输出建议流转人与其 @ 写法
 *   node scripts/jira-cli.mjs attach-list <KEY|URL>          # 列出该单现有附件
 *
 * 令牌来源：--token 参数、JIRA_TOKEN 环境变量，或 jira.mjs 默认查找的令牌文件。
 */

import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import * as jira from "./jira.mjs";


// 令牌文件候选名，与 jira.mjs 保持一致；这里用跨平台路径拼接，兼容 Linux 容器。
const TOKEN_FILE_NAMES = [".jira-token", ".jira-token.txt", "jira-token.txt"];


const USAGE = `用法（只读预演版，不提供任何写 Jira 的命令）：
  node scripts/jira-cli.mjs selftest
  node scripts/jira-cli.mjs precheck <KEY|URL>
  node scripts/jira-cli.mjs get <KEY|URL>
  node scripts/jira-cli.mjs json <KEY|URL>
  node scripts/jira-cli.mjs attachments <KEY|URL> <目录>
  node scripts/jira-cli.mjs reviews <KEY|URL>
  node scripts/jira-cli.mjs gate-get <KEY|URL>
  node scripts/jira-cli.mjs transitions <KEY|URL>
  node scripts/jira-cli.mjs flow-owner <KEY|URL>
  node scripts/jira-cli.mjs attach-list <KEY|URL>

打包交付：node scripts/pack-deliverables.mjs --key <KEY> --comment <评论草稿> --report <报告md>`;


/**
 * 解析命令行参数，支持 --key value 形式的选项与位置参数。
 */
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


/**
 * 在技能目录及其上级目录查找令牌文件，返回解析后的 { token, baseUrl }。
 *
 * jira.mjs 的文件查找使用 Windows 风格路径拼接，在 Linux 容器中不适用，
 * 因此命令行入口单独实现一份跨平台版本作为兜底。
 */
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


/**
 * 组装 jira.mjs 需要的调用选项，命令行优先于环境变量。
 */
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


/**
 * 校验位置参数数量，缺失时给出明确的使用提示。
 */
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
      // Jira 站点可能禁止普通账号访问 /myself；只要站点可达，仍应继续用 get 验证取数能力。
      const accountStep = (result.steps || []).find((item) => item.step === "校验令牌");
      if (accountStep && !accountStep.ok) {
        result.hint = "若“访问 Jira”步骤已成功，说明地址与网络可用；/myself 返回 401 通常只是该令牌无权访问账户接口，请改用 get <KEY|URL> 验证取数，不要因此判定令牌失效。";
      }
      console.log(JSON.stringify(result, null, 2));
      return;
    }
    case "precheck": {
      requireArgs(positional, 1, "precheck <KEY|URL>");
      // 只读类型预检：类型不是「故事」时由调用方立即停止，不产生任何 Jira 写操作。
      console.log(JSON.stringify(await jira.precheckIssueType(positional[0], callOptions), null, 2));
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
    case "attachments": {
      requireArgs(positional, 2, "attachments <KEY|URL> <目录>");
      const saved = await jira.downloadAttachments(positional[0], positional[1], callOptions);
      console.log(JSON.stringify(saved, null, 2));
      return;
    }
    case "reviews": {
      requireArgs(positional, 1, "reviews <KEY|URL>");
      console.log(JSON.stringify(await jira.findPreviousReviews(positional[0], callOptions), null, 2));
      return;
    }
    case "gate-get": {
      requireArgs(positional, 1, "gate-get <KEY|URL>");
      console.log(JSON.stringify(await jira.getGateResult(positional[0], callOptions), null, 2));
      return;
    }
    case "transitions": {
      requireArgs(positional, 1, "transitions <KEY|URL>");
      console.log(JSON.stringify(await jira.listTransitions(positional[0], callOptions), null, 2));
      return;
    }
    case "flow-owner": {
      requireArgs(positional, 1, "flow-owner <KEY|URL>");
      // 先取单子再解析人员字段，避免调用方重复请求；结果只用于写出「建议 @ 谁」，不实际 @。
      const issue = await jira.getIssue(positional[0], callOptions);
      const owner = await jira.resolveFlowOwner(issue, callOptions);
      console.log(JSON.stringify({ key: issue.key, status: issue.status, ...owner }, null, 2));
      return;
    }
    case "attach-list": {
      requireArgs(positional, 1, "attach-list <KEY|URL>");
      const attachmentsIssue = await jira.getIssue(positional[0], callOptions);
      console.log(JSON.stringify(attachmentsIssue.attachments, null, 2));
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
