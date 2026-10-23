#!/usr/bin/env node
/**
 * analysis-cli.mjs —— 缺陷单门禁技能调用故障分析接口的命令行入口。
 *
 * 用法：
 *   node scripts/analysis-cli.mjs selftest
 *   node scripts/analysis-cli.mjs resolve --product <名称|ID> --module <名称|ID> [--version <Tag>] [--related <模块:Tag> ...] [--config <映射文件>]
 *   node scripts/analysis-cli.mjs analyze --dir <附件目录> --product <名称|ID> --module <名称|ID> \
 *       [--branch <仓库地址>] [--version <Tag>] [--related <模块:Tag> ...] [--config <映射文件>] [--out <报告.html>] [--key <缺陷单号>] [--jira-url <地址>] \
 *       [--image-tag log_image] [--image-description <说明>] [--date-filter <YYYY-MM-DD>] [--timeout 1800]
 *
 * 接口地址：--base-url 优先，其次 ANALYSIS_API_BASE_URL；同机未配置时按 API_PORT 自动使用回环地址。
 * 令牌：--token 或 ANALYSIS_API_TOKEN 仅在非回环地址调用时需要，回环内部调用可为空。
 */

import { readFileSync } from "node:fs";

import * as analysis from "./analysis.mjs";
import { buildScopeFailurePayload } from "./scope.mjs";


const USAGE = `用法：
  node scripts/analysis-cli.mjs selftest
  node scripts/analysis-cli.mjs resolve --product <名称|ID> --module <名称|ID> [--version <Tag>] [--related <模块:Tag> ...] [--config <映射文件>]
  node scripts/analysis-cli.mjs analyze --dir <附件目录> --product <名称|ID> --module <名称|ID>
        [--branch <仓库地址>] [--version <Tag>] [--config <映射文件>] [--out <报告.html>] [--key <缺陷单号>]
        [--related <模块:Tag> ...]
        [--jira-url <地址>] [--image-tag log_image] [--image-description <说明>]
        [--date-filter <YYYY-MM-DD>] [--timeout 1800] [--json-summary <文件>]`;


/** 解析命令行参数，支持 --key value 与位置参数 */
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
        if (key === "related" && options[key] !== undefined) {
          options[key] = [].concat(options[key], next);
        } else {
          options[key] = next;
        }
        index += 1;
      }
    } else {
      positional.push(item);
    }
  }
  return { positional, options };
}


/** 解析可重复的 --related 参数：模块名:Tag。 */
function parseRelatedOptions(options) {
  const raw = options.related === undefined ? [] : [].concat(options.related);
  const result = [];
  for (const item of raw) {
    for (const spec of String(item || "").split(/\s*,\s*|\s+/).filter(Boolean)) {
      const separator = spec.indexOf(":");
      if (separator <= 0 || separator === spec.length - 1) {
        throw new Error(`--related 格式错误：${spec}；应为 <模块:Tag>`);
      }
      result.push({ module: spec.slice(0, separator), version: spec.slice(separator + 1) });
    }
  }
  return result;
}


/** 组装接口调用参数 */
function callOptions(options) {
  const result = {};
  if (typeof options["base-url"] === "string") result.baseUrl = options["base-url"];
  if (typeof options.token === "string") result.token = options.token;
  if (typeof options.config === "string") result.configPath = options.config;
  return result;
}


/** 输出进度（每行一条，便于在任务日志里查看） */
function progressPrinter(options) {
  if (options.quiet) return undefined;
  return (event) => {
    const percent = event.percent === undefined ? "" : ` ${event.percent}%`;
    console.log(`[analysis]${percent} ${event.message || event.stage || ""}`.trim());
  };
}


async function main() {
  const { positional, options } = parseArgs(process.argv.slice(2));
  const command = positional.shift();
  if (!command || command === "help" || options.help) {
    console.log(USAGE);
    return;
  }
  const callArgs = callOptions(options);

  switch (command) {
    case "selftest": {
      console.log(JSON.stringify(await analysis.selftest(callArgs), null, 2));
      return;
    }
    case "resolve": {
      const scope = await analysis.resolveScope({
        product: options.product,
        module: options.module,
        branch: typeof options.branch === "string" ? options.branch : undefined,
        version: typeof options.version === "string" ? options.version : undefined,
        related: parseRelatedOptions(options),
      }, callArgs);
      await analysis.validateScopeTags(scope, callArgs);
      console.log(JSON.stringify(scope, null, 2));
      return;
    }
    case "analyze": {
      if (typeof options.dir !== "string" || !options.dir) {
        throw new Error("缺少附件目录：请使用 --dir <附件目录>");
      }
      const scope = await analysis.resolveScope({
        product: options.product,
        module: options.module,
        branch: typeof options.branch === "string" ? options.branch : undefined,
        version: typeof options.version === "string" ? options.version : undefined,
        related: parseRelatedOptions(options),
      }, callArgs);
      await analysis.validateScopeTags(scope, callArgs);
      console.log(`[analysis] 分析范围：产品=${scope.productName || scope.productId} 模块=${scope.moduleName || scope.moduleId} Tag=${scope.tagVersion} 关联模块=${(scope.relatedModules || []).length} 公共组件候选=${(scope.commonModules || []).length}`);

      const payload = await analysis.analyzeDirectory(options.dir, { scope }, {
        ...callArgs,
        dateFilter: typeof options["date-filter"] === "string" ? options["date-filter"] : undefined,
        imageTag: typeof options["image-tag"] === "string" ? options["image-tag"] : undefined,
        imageDescription: typeof options["image-description"] === "string" ? options["image-description"] : undefined,
        timeoutSeconds: options.timeout ? Number(options.timeout) : undefined,
        onProgress: progressPrinter(options),
      });

      const report = {
        issueKey: typeof options.key === "string" ? options.key : "",
        jiraUrl: typeof options["jira-url"] === "string" ? options["jira-url"] : "",
        generatedAt: new Date().toISOString(),
        scope: payload.scope,
        files: payload.files,
        notes: payload.notes,
        tasks: payload.tasks,
      };
      const outPath = typeof options.out === "string" && options.out
        ? options.out
        : String(options.dir).replace(/\/+$/, "") + "/fault-analysis-report.html";
      await analysis.writeReport(report, outPath);

      const summary = analysis.buildSummary(report);
      const failed = report.tasks.filter((task) => task.error);
      const result = {
        ok: failed.length === 0,
        report_path: outPath,
        summary,
        scope: report.scope,
        tasks: report.tasks.map((task) => ({ label: task.label, task_id: task.taskId, error: task.error || null })),
      };
      if (typeof options["json-summary"] === "string" && options["json-summary"]) {
        const fs = await import("node:fs");
        fs.writeFileSync(options["json-summary"], JSON.stringify(result, null, 2), "utf8");
      }
      console.log(JSON.stringify(result, null, 2));
      if (failed.length) process.exitCode = 2;
      return;
    }
    case "render": {
      // 用既有结果 JSON 重新渲染报告，便于调试样式（不调用网络）。
      if (typeof options.input !== "string" || !options.input) throw new Error("缺少 --input <结果JSON>");
      const payload = JSON.parse(readFileSync(options.input, "utf8"));
      const outPath = typeof options.out === "string" && options.out ? options.out : "fault-analysis-report.html";
      await analysis.writeReport(payload, outPath);
      console.log(JSON.stringify({ ok: true, report_path: outPath }, null, 2));
      return;
    }
    default:
      throw new Error(`未知命令：${command}\n${USAGE}`);
  }
}


main().catch((error) => {
  const { options } = parseArgs(process.argv.slice(2));
  const failure = buildScopeFailurePayload(error, callOptions(options).configPath);
  if (failure.skipped) {
    console.log(JSON.stringify(failure, null, 2));
    return;
  }
  console.error(failure.message);
  process.exit(1);
});
