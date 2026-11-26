#!/usr/bin/env node
/**
 * jira-gate-1-local 独立自检。
 *
 * 用法：node scripts/self-check.mjs
 *
 * 检查内容：
 * 1. 在包外工作目录、未设置 JIRA_GATE_LOCAL_MODE 时仍自动进入本地预检模式；
 * 2. 主技能与 QA 子技能都会拦截写评论；
 * 3. 即使漏复制 .local-preview，SKILL.md 声明仍能保持只读；
 * 4. 本地评论草稿可以正常打包；
 * 5. 本地附件优先，不访问 Jira。
 *
 * 本脚本只使用 Node 内置模块和包内文件，不访问网络、不读取凭据。
 */

import { spawnSync } from "node:child_process";
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const skillRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const failures = [];
const lines = [];

/** 执行单项检查，失败时记录原因但不中断后续检查。 */
async function check(name, fn) {
  try {
    await fn();
    lines.push(`[OK] ${name}`);
  } catch (error) {
    lines.push(`[FAIL] ${name}: ${error?.message || error}`);
    failures.push(name);
  }
}

/** 断言条件成立。 */
function assert(condition, message) {
  if (!condition) throw new Error(message);
}

const originalCwd = process.cwd();
const temp = mkdtempSync(join(tmpdir(), "jira-gate-1-local-selfcheck-"));

// 模拟“把目录拿到本机后”的场景：工作目录在包外，且不设置本地模式环境变量。
process.chdir(temp);
delete process.env.JIRA_GATE_LOCAL_MODE;

const mainJiraPath = join(skillRoot, "scripts", "jira.mjs");
const qaRoot = join(skillRoot, "subskills", "review-jira-qa");
const qaEnvPath = join(qaRoot, "bin", "lib", "env.mjs");
const qaJiraPath = join(qaRoot, "bin", "lib", "jira.mjs");
const packPath = join(skillRoot, "scripts", "pack-local-deliverables.mjs");
const preparePath = join(skillRoot, "scripts", "prepare-attachments.mjs");

const mainJira = await import(pathToFileURL(mainJiraPath).href);
const qaEnv = await import(pathToFileURL(qaEnvPath).href);
const qaJira = await import(pathToFileURL(qaJiraPath).href);

await check("包外工作目录自动进入本地预检模式", async () => {
  assert(await mainJira.isLocalGateMode(), "主技能 isLocalGateMode() 返回 false");
  assert(qaEnv.isLocalGateMode(), "QA 子技能 isLocalGateMode() 返回 false");
});

await check("主技能拦截写评论", async () => {
  let blocked = false;
  try {
    await mainJira.addComment("TEST-1", "should-be-blocked", {
      token: "not-used",
      baseUrl: "https://jira.invalid.example.com",
    });
  } catch (error) {
    blocked = /禁止向 Jira 写入/.test(String(error?.message || error));
  }
  assert(blocked, "addComment 未被拦截");
});

await check("QA 子技能拦截写评论", async () => {
  let blocked = false;
  try {
    await new qaJira.JiraClient({
      baseUrl: "https://jira.invalid.example.com",
      token: "not-used",
    }).createComment("TEST-1", "should-be-blocked");
  } catch (error) {
    blocked = /禁止向 Jira 写入/.test(String(error?.message || error));
  }
  assert(blocked, "createComment 未被拦截");
});

await check("漏复制 .local-preview 时仍按 SKILL.md 保持只读", async () => {
  const copyRoot = join(temp, "copy-without-marker");
  mkdirSync(join(copyRoot, "scripts"), { recursive: true });
  mkdirSync(join(copyRoot, "subskills", "review-jira-qa", "bin", "lib"), { recursive: true });
  copyFileSync(join(skillRoot, "SKILL.md"), join(copyRoot, "SKILL.md"));
  copyFileSync(mainJiraPath, join(copyRoot, "scripts", "jira.mjs"));
  copyFileSync(join(qaRoot, "SKILL.md"), join(copyRoot, "subskills", "review-jira-qa", "SKILL.md"));
  copyFileSync(qaEnvPath, join(copyRoot, "subskills", "review-jira-qa", "bin", "lib", "env.mjs"));

  const stamp = Date.now();
  const copyMain = await import(`${pathToFileURL(join(copyRoot, "scripts", "jira.mjs")).href}?v=${stamp}`);
  const copyQaEnv = await import(
    `${pathToFileURL(join(copyRoot, "subskills", "review-jira-qa", "bin", "lib", "env.mjs")).href}?v=${stamp}`
  );
  assert(await copyMain.isLocalGateMode(), "复制包主技能返回 false");
  assert(copyQaEnv.isLocalGateMode(), "复制包 QA 子技能返回 false");
});

await check("本地评论草稿可以打包", () => {
  const comment = join(temp, "comment.txt");
  writeFileSync(comment, "h3. G1 需求准入结论（本地预检）：通过\n# (/) *5.1 事实源*（MUST）\n", "utf8");
  const pack = spawnSync(
    process.execPath,
    [packPath, "--key", "TEST-1", "--comment", comment, "--no-report", "--out", join(temp, "outputs")],
    { encoding: "utf8", cwd: temp },
  );
  assert(pack.status === 0, `退出码 ${pack.status}：${String(pack.stderr || "").trim()}`);
  const result = JSON.parse(pack.stdout);
  assert(result.ok && existsSync(result.zip), "ZIP 未生成");
  assert(result.entries.length === 1, `条目数量不正确：${result.entries.length}`);
});

await check("本地附件优先且不访问 Jira", () => {
  const sourceDir = join(temp, "local-attachments");
  mkdirSync(sourceDir, { recursive: true });
  writeFileSync(join(sourceDir, "TEST-1-PRD_DELIVERY.md"), "# PRD\n", "utf8");
  const workspace = join(temp, "workspace");
  const prepared = spawnSync(
    process.execPath,
    [preparePath, "--key", "TEST-1", "--workspace", workspace, "--local", sourceDir],
    { encoding: "utf8", cwd: temp },
  );
  assert(prepared.status === 0, String(prepared.stderr || "").trim());
  const result = JSON.parse(prepared.stdout);
  assert(result.source === "local", `source=${result.source}`);
  assert(result.files.some((item) => item.name === "TEST-1-PRD_DELIVERY.md"), "本地附件未复制");
});

await check("预检包拒绝写入技能包内部", () => {
  const comment = join(temp, "comment-nested.txt");
  writeFileSync(comment, "h3. G1 需求准入结论（本地预检）：通过\n# (/) *5.1 事实源*（MUST）\n", "utf8");
  const nestedOut = join(skillRoot, "outputs");
  const pack = spawnSync(
    process.execPath,
    [packPath, "--key", "TEST-1", "--comment", comment, "--no-report", "--out", nestedOut],
    { encoding: "utf8", cwd: temp },
  );
  assert(pack.status !== 0, "写进技能包内部时未失败");
  assert(/out-inside-skill/.test(String(pack.stderr || "")), "未给出 out-inside-skill 原因");
  assert(!existsSync(nestedOut), "技能包内被创建了 outputs 目录");
});

await check("附件工作区拒绝指向技能包内部", () => {
  const sourceDir = join(temp, "local-attachments-2");
  mkdirSync(sourceDir, { recursive: true });
  writeFileSync(join(sourceDir, "TEST-1-PRD_DELIVERY.md"), "# PRD\n", "utf8");
  const prepared = spawnSync(
    process.execPath,
    [preparePath, "--key", "TEST-1", "--workspace", join(skillRoot, "work"), "--local", sourceDir],
    { encoding: "utf8", cwd: temp },
  );
  assert(prepared.status !== 0, "工作区指向技能包内部时未失败");
  assert(/工作区不能位于技能包内/.test(String(prepared.stderr || "")), "未给出工作区拦截原因");
});

await check("QA 评论脚本在本地模式强制 dry-run", () => {
  const comment = join(temp, "qa-comment.txt");
  writeFileSync(comment, "h3. QA-需求准入门禁结果（本地预检）\n", "utf8");
  const env = { ...process.env, CLAUDE_SKILL_DIR: qaRoot, QA_WORKSPACE: join(temp, "qa-ws") };
  delete env.JIRA_BASE_URL;
  delete env.JIRA_PAT;
  delete env.JIRA_TOKEN;
  delete env.JIRA_GATE_LOCAL_MODE;
  const run = spawnSync(
    process.execPath,
    [join(qaRoot, "bin", "jira-comment.mjs"), "--key", "TEST-1", "--body", comment, "--run-id", "selfcheck", "--stage", "risk-analysis"],
    { encoding: "utf8", cwd: temp, env },
  );
  assert(run.status === 0, `退出码 ${run.status}：${String(run.stderr || "").trim()}`);
  assert(/DRY_RUN=1/.test(String(run.stdout || "")), "未输出 DRY_RUN=1");
});

process.chdir(originalCwd);
rmSync(temp, { recursive: true, force: true });

for (const line of lines) process.stdout.write(`${line}\n`);
if (failures.length) {
  process.stderr.write(`\nSELF_CHECK_FAILED（${failures.length} 项）\n`);
  process.exitCode = 1;
} else {
  process.stdout.write("\nSELF_CHECK_OK\n");
}
