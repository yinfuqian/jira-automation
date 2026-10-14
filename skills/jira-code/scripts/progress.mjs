#!/usr/bin/env node
/**
 * progress.mjs —— jira-code 的进度上报器：在 Jira 单里维护「唯一一条」进度评论。
 *
 * 用法：
 *   node scripts/progress.mjs start  --issue <KEY|URL> --state <state.json> [--stage 文本] [--summary 摘要]
 *                                    [--repo <检出目录>] [--repo-label <仓库名>] [--branch <分支>] [--interval 300]
 *   node scripts/progress.mjs update --state <state.json> [--stage 文本] [--step 文本] [--status running]
 *                                    [--hotreload <pending|succeeded|skipped|failed>] [--hotreload-reason 文本]
 *   node scripts/progress.mjs finish --state <state.json> [--message 文本]
 *                                    [--hotreload <...>] [--hotreload-reason 文本]
 *   node scripts/progress.mjs fail   --state <state.json> --message <中断/异常原因>
 *                                    [--hotreload <...>] [--hotreload-reason 文本]
 *   node scripts/progress.mjs tick   --state <state.json>      # 立刻刷新一次（心跳内部也走这条路径）
 *   node scripts/progress.mjs stop   --state <state.json>      # 只停心跳，不改评论
 *   node scripts/progress.mjs show   --state <state.json>      # 打印状态文件，便于排查
 *
 * 关键约定：
 *   1. 一条需求单只允许一条进度评论：评论里带固定标记行 `jira-code:progress:<KEY>`，
 *      写入前先按标记查找，找到就 PUT 更新，找不到才 POST 新建 —— 重复执行本技能不会刷屏。
 *   2. `start` 会立刻发布一次，并 fork 一个脱离父进程的后台心跳进程，默认每 300 秒刷新一次，
 *      所以 agent 在长时间写代码时，Jira 上的进度也会自己往前走。
 *   3. 进度与异常都写进同一条评论；异常不会新开评论，而是把这条评论的「状态」改成已中断并追加原因。
 *   4. `finish` / `fail` 会同步发布最终状态并停掉心跳；写入失败时以退出码 1 告警（不要把失败说成成功）。
 *   5. 「热更新结论」是评论里固定渲染的一行，任何收尾路径都不会省略：热更新阶段无论执行成功、跳过
 *      还是失败，都要用 `--hotreload` 上报；`finish` / `fail` 时仍未上报的由脚本兜底成「未执行」，
 *      避免评论看起来像「全部完成」而漏掉没做热更新这件事。
 */

import { spawn } from "node:child_process";
import { closeSync, openSync, readFileSync, renameSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

import * as jira from "./jira.mjs";
import { snapshot } from "./git-flow.mjs";


// 评论里的固定标记：既能被人读到，也能被脚本用来定位「本单已有的进度评论」。
const MARKER_PREFIX = "jira-code:progress:";
// 进度明细最多保留多少条，保证评论简短。
const MAX_STEPS = 8;
const MAX_STEP_CHARS = 200;
const DEFAULT_INTERVAL_SECONDS = 300;
// 热更新结论的状态枚举：pending 表示还没走到热更新阶段。
const HOTRELOAD_STATUSES = ["pending", "succeeded", "skipped", "failed"];
// 上报用词到标准状态的映射：agent 用中文或英文写都能识别，识别不了会直接报错而不是静默丢弃。
const HOTRELOAD_ALIASES = {
  pending: "pending",
  待执行: "pending",
  未开始: "pending",
  succeeded: "succeeded",
  success: "succeeded",
  ok: "succeeded",
  成功: "succeeded",
  已完成: "succeeded",
  skipped: "skipped",
  skip: "skipped",
  跳过: "skipped",
  未执行: "skipped",
  不适用: "skipped",
  不涉及: "skipped",
  failed: "failed",
  fail: "failed",
  error: "failed",
  失败: "failed",
  异常: "failed",
};
// 结论行里的展示文案。
const HOTRELOAD_LABELS = { pending: "待执行", succeeded: "已完成", skipped: "未执行", failed: "失败" };
// 收尾时仍未上报的兜底原因：宁可写成「未执行」，也不能让评论漏掉热更新。
const HOTRELOAD_MISSING_REASON = {
  finish: "未上报，视为未执行（未进入热更新阶段）",
  fail: "未上报，视为未执行（任务中断，热更新未完成）",
};
// 心跳最长存活时间（分钟），超过就自动收尾退出，避免异常情况下留下永久后台进程。
const DEFAULT_MAX_MINUTES = 180;
const LOCK_STALE_MS = 120000;
const LOCK_WAIT_MS = 15000;
const STATE_VERSION = 1;


/** 当前时间的可读写法（默认按 Asia/Shanghai 显示，容器时区不影响评论可读性）。 */
function now() {
  const timeZone = process.env.JIRA_CODE_TZ || "Asia/Shanghai";
  try {
    const parts = new Intl.DateTimeFormat("sv-SE", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(new Date());
    return parts.replace("T", " ");
  } catch (error) {
    return new Date().toISOString().slice(0, 16).replace("T", " ");
  }
}


/** 只截取时分，用于进度明细行。 */
function clock() {
  const text = now();
  return text.length >= 16 ? text.slice(11, 16) : text;
}


function sleep(ms) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, ms));
}


function parseArgs(argv) {
  const positional = [];
  const options = {};
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (item.startsWith("--")) {
      const next = argv[index + 1];
      if (next === undefined || String(next).startsWith("--")) {
        options[item.slice(2)] = true;
      } else {
        options[item.slice(2)] = next;
        index += 1;
      }
    } else {
      positional.push(item);
    }
  }
  return { positional, options };
}


function statePathOf(options, required = true) {
  const value = options.state ? String(options.state) : "";
  if (!value) {
    if (!required) return "";
    throw new Error("缺少 --state <状态文件路径>");
  }
  return resolve(value);
}


function readState(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    return null;
  }
}


function writeState(path, state) {
  // 先写临时文件再改名，避免心跳与 agent 同时写时读到半截 JSON。
  const temp = `${path}.tmp`;
  writeFileSync(temp, JSON.stringify(state, null, 2), "utf8");
  try {
    renameSync(temp, path);
  } catch (error) {
    // Windows 上 rename 不能覆盖已存在的文件，先删目标再改名。
    try {
      unlinkSync(path);
    } catch (unlinkError) {
      // 目标文件不存在时忽略。
    }
    renameSync(temp, path);
  }
}


/** 互斥锁：同一时刻只允许一个进程发布评论，避免并发新建出两条评论。 */
async function withLock(path, handler) {
  const lockPath = `${path}.lock`;
  const startedAt = Date.now();
  let handle = null;
  while (true) {
    try {
      handle = openSync(lockPath, "wx");
      writeFileSync(lockPath, `${process.pid}\n${Date.now()}\n`, "utf8");
      break;
    } catch (error) {
      // 锁文件可能是崩溃残留：超过阈值直接抢过来。
      try {
        const info = statSync(lockPath);
        if (Date.now() - info.mtimeMs > LOCK_STALE_MS) {
          unlinkSync(lockPath);
          continue;
        }
      } catch (statError) {
        continue;
      }
      if (Date.now() - startedAt > LOCK_WAIT_MS) throw new Error(`等待进度锁超时：${lockPath}`);
      await sleep(80);
    }
  }
  try {
    return await handler();
  } finally {
    try {
      closeSync(handle);
    } catch (error) {
      // 句柄已关闭时忽略。
    }
    try {
      unlinkSync(lockPath);
    } catch (error) {
      // 锁文件被抢走/删除时忽略。
    }
  }
}


/** 把上报用词归一成标准状态；无法识别时抛错，避免结论被悄悄丢掉。 */
function normalizeHotreload(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  const hit = HOTRELOAD_ALIASES[text] || HOTRELOAD_ALIASES[text.toLowerCase()];
  if (!hit) {
    throw new Error(`无法识别的热更新状态：${text}（可用值：${HOTRELOAD_STATUSES.join(" / ")}）`);
  }
  return hit;
}


/** 写入热更新结论；reason 留空时沿用上一次的原因。 */
function setHotreload(state, status, reason) {
  const normalized = normalizeHotreload(status);
  if (!normalized) return false;
  const text = String(reason || "").trim().slice(0, 200);
  state.hotreload = {
    status: normalized,
    reason: text || (state.hotreload && state.hotreload.reason) || "",
    at: now(),
  };
  return true;
}


/** 收尾兜底：热更新没有结论（或还停在 pending）时一律记为未执行，保证评论不漏报。 */
function ensureHotreloadReported(state, finalStatus) {
  const current = state.hotreload && HOTRELOAD_STATUSES.includes(state.hotreload.status) ? state.hotreload : null;
  if (current && current.status !== "pending") {
    if (!current.at) current.at = now();
    return false;
  }
  state.hotreload = {
    status: "skipped",
    reason: HOTRELOAD_MISSING_REASON[finalStatus === "aborted" ? "fail" : "finish"],
    at: now(),
  };
  return true;
}


/** 热更新结论行：进行中显示待执行，收尾后一定是明确结论。 */
function hotreloadLine(state) {
  const stored = state.hotreload && HOTRELOAD_STATUSES.includes(state.hotreload.status) ? state.hotreload : null;
  const settled = state.status === "done" || state.status === "aborted";
  const status = stored ? stored.status : settled ? "skipped" : "pending";
  // 旧状态文件没有这个字段时，收尾态要补上未执行的原因，不能只写一个「未执行」。
  const reason = (stored && stored.reason) || (!stored && settled ? HOTRELOAD_MISSING_REASON.finish : "");
  return `* 热更新：*${HOTRELOAD_LABELS[status]}*${reason ? `｜${reason}` : ""}`;
}


/** 渲染评论正文：Jira wiki 标记，内容保持简短。 */
export function renderBody(state) {
  const statusLabel = state.status === "done" ? "已完成" : state.status === "aborted" ? "已中断" : "进行中";
  const key = state.issueKey || "";
  const lines = [];
  lines.push(`h4. jira-code 自动开发进度（${key}${state.summary ? " " + state.summary : ""}）`);
  lines.push("");
  lines.push(`* 状态：*${statusLabel}*`);
  lines.push(`* 当前阶段：${state.stage || "准备中"}`);
  // 热更新结论固定渲染：缺字段、跳过、失败都要在评论里一眼看到。
  lines.push(hotreloadLine(state));
  if (state.repoLabel || state.branch) {
    lines.push(`* 代码分支：${state.branch || "（未创建）"}${state.repoLabel ? "（" + state.repoLabel + "）" : ""}`);
  }
  lines.push(`* 开始时间：${state.startedAt || ""}｜最近更新：${state.updatedAt || now()}（每 ${Math.round((state.intervalSeconds || DEFAULT_INTERVAL_SECONDS) / 60)} 分钟自动刷新）`);

  const steps = Array.isArray(state.steps) ? state.steps.slice(-MAX_STEPS) : [];
  if (steps.length) {
    lines.push("");
    lines.push("h5. 进度明细");
    steps.forEach((step, index) => {
      lines.push(`${index + 1}. [${step.at}] ${step.text}`);
    });
  }

  // 心跳时顺手带上代码快照，让评论反映真实进展而不是只有一句「进行中」。
  if (state.repo) {
    const info = snapshot(state.repo);
    lines.push("");
    lines.push("h5. 代码改动快照");
    if (info.ok) {
      lines.push(`* 分支：${info.branch || "（未知）"}｜提交数：${info.commits}｜未提交改动：${info.changedFiles} 个文件`);
      if (info.head) lines.push(`* 最近提交：${info.head}`);
    } else {
      lines.push(`* 暂未拿到代码快照（${info.reason || "未知原因"}）`);
    }
  }

  if (state.status === "aborted" && state.error) {
    lines.push("");
    lines.push(`* 异常：*${state.error}*`);
  }
  if (state.status === "done" && state.result) {
    lines.push("");
    lines.push(`* 结果：${state.result}`);
  }
  if (state.lastPublishError) {
    lines.push("");
    lines.push(`* 上次刷新失败：${state.lastPublishError}`);
  }
  lines.push("");
  lines.push(`本评论由 jira-code 技能自动维护，每次只更新这一条，不会新增进度评论。`);
  lines.push("");
  lines.push(`${MARKER_PREFIX}${key}`);
  return lines.join("\n");
}


/** 在已有评论里找出本单的进度评论（按固定标记匹配），找不到返回 null。 */
async function findProgressComment(key, options = {}) {
  const issue = await jira.getIssue(key, options);
  const comments = Array.isArray(issue.comments) ? issue.comments : [];
  const matches = comments.filter((comment) => {
    const body = String(comment.body || "");
    return body.includes(`${MARKER_PREFIX}${issue.key || key}`) || body.includes(MARKER_PREFIX);
  });
  if (!matches.length) return null;
  // 有多条（历史遗留）时以最后一条为准，并把最新内容写回它，避免再新增。
  return matches[matches.length - 1];
}


/** 发布：写入或更新那条唯一的进度评论，返回 { ok, commentId, created }。 */
async function publish(statePath, options = {}) {
  return withLock(statePath, async () => {
    const state = readState(statePath);
    if (!state) return { ok: false, reason: "state-missing" };
    const body = renderBody(state);
    let commentId = state.commentId || null;
    let created = false;

    if (commentId) {
      try {
        await jira.updateComment(state.issueKey, commentId, body, options);
      } catch (error) {
        // 评论被删除或 ID 失效时，退回「按标记查找 → 新建」，仍然只保留一条评论。
        commentId = null;
      }
    }
    if (!commentId) {
      const existing = await findProgressComment(state.issueKey, options);
      if (existing) {
        commentId = existing.id;
        await jira.updateComment(state.issueKey, commentId, body, options);
      } else {
        const createdComment = await jira.addComment(state.issueKey, body, options);
        commentId = createdComment && createdComment.id;
        created = true;
      }
    }

    state.commentId = commentId;
    state.updatedAt = now();
    state.publishCount = (state.publishCount || 0) + 1;
    state.lastPublishAt = state.updatedAt;
    state.lastPublishError = null;
    writeState(statePath, state);
    return { ok: true, commentId, created, publishCount: state.publishCount };
  });
}


/** 记录一次发布失败：既写进状态文件，也让评论下次能带上失败原因。 */
function recordPublishError(statePath, error) {
  const state = readState(statePath);
  if (!state) return;
  state.lastPublishError = String((error && error.message) || error).slice(0, 300);
  state.updatedAt = now();
  writeState(statePath, state);
}


function isDaemonAlive(pid) {
  const value = Number(pid);
  if (!value) return false;
  try {
    process.kill(value, 0);
    return true;
  } catch (error) {
    return false;
  }
}


/** 停掉心跳进程；进程已退出时静默返回。 */
function stopDaemon(state) {
  if (!state || !state.daemonPid) return { stopped: false };
  const pid = Number(state.daemonPid);
  try {
    process.kill(pid, "SIGTERM");
  } catch (error) {
    // 进程已不存在时忽略。
  }
  return { stopped: true, pid };
}


/** 后台心跳：每 interval 秒把最新状态刷进那条唯一的评论。 */
async function daemon(statePath, options) {
  const initial = readState(statePath);
  if (!initial) {
    process.stderr.write(`[进度心跳] 状态文件不存在，直接退出：${statePath}\n`);
    return;
  }
  const intervalSeconds = Math.max(1, Number(options.interval || initial.intervalSeconds || DEFAULT_INTERVAL_SECONDS));
  const maxMinutes = Math.max(1, Number(options["max-minutes"] || process.env.JIRA_CODE_PROGRESS_MAX_MINUTES || DEFAULT_MAX_MINUTES));
  const deadline = Date.now() + maxMinutes * 60000;
  process.stderr.write(`[进度心跳] 启动：每 ${intervalSeconds} 秒刷新一次，最长 ${maxMinutes} 分钟，状态文件 ${statePath}\n`);

  while (true) {
    await sleep(intervalSeconds * 1000);
    // 每轮都重新读状态：状态文件被删或任务已收尾就退出，绝不写回陈旧内容。
    const state = readState(statePath);
    if (!state) return;
    if (state.status !== "running") return;
    if (Date.now() > deadline) {
      state.status = "aborted";
      state.stage = "心跳超时";
      state.error = `进度心跳超过 ${maxMinutes} 分钟自动停止；任务可能已异常退出，请人工确认`;
      writeState(statePath, state);
      try {
        await publish(statePath, options);
      } catch (error) {
        recordPublishError(statePath, error);
      }
      return;
    }
    try {
      await publish(statePath, options);
    } catch (error) {
      // 单次刷新失败不影响后续心跳：写进状态，下一轮继续尝试。
      recordPublishError(statePath, error);
    }
  }
}


async function main() {
  const [command, ...rest] = process.argv.slice(2);
  const { options } = parseArgs(rest);
  const usage = "用法：node scripts/progress.mjs <start|update|finish|fail|tick|stop|show|_daemon> --state <文件> [选项]";

  switch (command) {
    case "start": {
      const statePath = statePathOf(options);
      const issueKey = jira.parseIssueKey(options.issue);
      if (!issueKey) throw new Error(`无法从 --issue 解析 Jira 单号：${options.issue}`);
      const previous = readState(statePath);
      const interval = Math.max(1, Number(options.interval || process.env.JIRA_CODE_PROGRESS_INTERVAL || DEFAULT_INTERVAL_SECONDS));
      const state = {
        version: STATE_VERSION,
        issueKey,
        status: "running",
        stage: String(options.stage || "准备中"),
        summary: String(options.summary || "").slice(0, 120),
        repo: options.repo ? resolve(String(options.repo)) : previous && previous.repo || "",
        repoLabel: String(options["repo-label"] || ""),
        branch: String(options.branch || ""),
        intervalSeconds: interval,
        startedAt: now(),
        updatedAt: now(),
        steps: [],
        // 热更新结论：新一次执行从「待执行」开始，收尾前必须被改成明确结论。
        hotreload: { status: "pending", reason: "", at: null },
        // 复用上一次的评论 ID：同一张单重跑时仍然只更新那一条评论。
        commentId: previous && previous.commentId ? previous.commentId : null,
        publishCount: previous && previous.publishCount ? previous.publishCount : 0,
        lastPublishError: null,
        daemonPid: null,
      };
      if (options.step) state.steps.push({ at: clock(), text: String(options.step).slice(0, MAX_STEP_CHARS) });
      writeState(statePath, state);

      let published;
      try {
        published = await publish(statePath, options);
      } catch (error) {
        recordPublishError(statePath, error);
        published = { ok: false, reason: "publish-failed", message: String((error && error.message) || error) };
      }

      // 已有存活心跳时不重复拉起，避免同一单跑出多个心跳进程。
      const current = readState(statePath) || state;
      let daemonPid = isDaemonAlive(current.daemonPid) ? current.daemonPid : null;
      if (!daemonPid) {
        const child = spawn(process.execPath, [fileURLToPath(import.meta.url), "_daemon", "--state", statePath, "--interval", String(interval)], {
          detached: true,
          stdio: "ignore",
          windowsHide: true,
        });
        child.unref();
        daemonPid = child.pid;
      }
      const latest = readState(statePath) || state;
      latest.daemonPid = daemonPid;
      latest.intervalSeconds = interval;
      writeState(statePath, latest);

      process.stdout.write(JSON.stringify({ ...published, stateFile: statePath, intervalSeconds: interval, daemonPid }, null, 2) + "\n");
      if (!published.ok) {
        process.stderr.write(`[进度评论写入失败] ${published.message || published.reason}\n`);
        process.exitCode = 1;
      }
      return;
    }

    case "update": {
      const statePath = statePathOf(options);
      const result = await withLock(statePath, async () => {
        const state = readState(statePath);
        if (!state) throw new Error(`状态文件不存在：${statePath}`);
        if (options.stage) state.stage = String(options.stage).slice(0, 120);
        if (options.status && ["running", "done", "aborted"].includes(String(options.status))) {
          state.status = String(options.status);
        }
        if (options.message) state.error = String(options.message).slice(0, 400);
        if (options.result) state.result = String(options.result).slice(0, 400);
        if (options.hotreload || options["hotreload-reason"]) {
          setHotreload(state, options.hotreload || (state.hotreload && state.hotreload.status) || "pending", options["hotreload-reason"]);
        }
        if (options.step) {
          state.steps = Array.isArray(state.steps) ? state.steps : [];
          state.steps.push({ at: clock(), text: String(options.step).slice(0, MAX_STEP_CHARS) });
          state.steps = state.steps.slice(-MAX_STEPS);
        }
        state.updatedAt = now();
        writeState(statePath, state);
        return state;
      });
      const published = await publish(statePath, options).catch((error) => {
        recordPublishError(statePath, error);
        return { ok: false, reason: "publish-failed", message: String((error && error.message) || error) };
      });
      process.stdout.write(JSON.stringify({ ...published, stage: result.stage, status: result.status, steps: (result.steps || []).length, hotreload: (result.hotreload && result.hotreload.status) || "pending" }, null, 2) + "\n");
      if (!published.ok) {
        process.stderr.write(`[进度评论写入失败] ${published.message || published.reason}\n`);
        process.exitCode = 1;
      }
      return;
    }

    case "finish":
    case "fail": {
      const statePath = statePathOf(options);
      const finalStatus = command === "finish" ? "done" : "aborted";
      const message = String(options.message || options.result || "").trim();
      const state = readState(statePath);
      stopDaemon(state);
      await withLock(statePath, async () => {
        const current = readState(statePath);
        if (!current) throw new Error(`状态文件不存在：${statePath}`);
        current.status = finalStatus;
        current.stage = command === "finish" ? "已结束" : String(options.stage || "已中断");
        current.daemonPid = null;
        current.updatedAt = now();
        if (command === "finish") {
          if (message) current.result = message.slice(0, 400);
          current.error = null;
        } else if (message) {
          current.error = message.slice(0, 400);
        }
        // 收尾允许在同一次调用里补报热更新结论，再对未上报的做兜底。
        if (options.hotreload || options["hotreload-reason"]) {
          setHotreload(current, options.hotreload || (current.hotreload && current.hotreload.status) || "pending", options["hotreload-reason"]);
        }
        ensureHotreloadReported(current, finalStatus);
        writeState(statePath, current);
      });
      let published;
      try {
        published = await publish(statePath, options);
      } catch (error) {
        recordPublishError(statePath, error);
        published = { ok: false, reason: "publish-failed", message: String((error && error.message) || error) };
      }
      process.stdout.write(JSON.stringify({ ...published, status: finalStatus, stage: command === "finish" ? "已结束" : (options.stage || "已中断") }, null, 2) + "\n");
      if (!published.ok) {
        process.stderr.write(`[进度评论写入失败] ${published.message || published.reason}\n`);
        process.exitCode = 1;
      }
      return;
    }

    case "tick": {
      const statePath = statePathOf(options);
      const published = await publish(statePath, options);
      process.stdout.write(JSON.stringify(published, null, 2) + "\n");
      return;
    }

    case "stop": {
      const statePath = statePathOf(options);
      const state = readState(statePath);
      const stopped = stopDaemon(state);
      if (state) {
        state.daemonPid = null;
        writeState(statePath, state);
      }
      process.stdout.write(JSON.stringify({ ok: true, ...stopped }, null, 2) + "\n");
      return;
    }

    case "show": {
      const statePath = statePathOf(options);
      const state = readState(statePath);
      if (!state) throw new Error(`状态文件不存在：${statePath}`);
      process.stdout.write(JSON.stringify(state, null, 2) + "\n");
      return;
    }

    case "_daemon":
      await daemon(statePathOf(options), options);
      return;

    default:
      throw new Error(usage);
  }
}


main().catch((error) => {
  process.stderr.write(`[进度上报失败] ${(error && error.message) || error}\n`);
  process.exitCode = 1;
});
