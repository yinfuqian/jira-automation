"""Codex 执行器测试：覆盖提示词、命令行构造、事件解析与子进程执行。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.skillrun.codex_runner import (
    build_codex_command,
    build_codex_env,
    build_network_failure_message,
    build_skill_prompt,
    is_network_error,
    normalize_base_url,
    redact_command,
    run_codex_skill,
    summarize_event,
)


class SkillPromptTests(unittest.TestCase):
    def make_skill(self, prompt_template=None):
        """构造测试用技能定义。"""
        return SimpleNamespace(
            skill_id="jira-gate-1",
            name="jira-gate-1",
            directory="/data/skills/jira-gate-1",
            prompt_template=prompt_template,
        )

    def test_default_prompt_contains_skill_jira_url_and_inputs(self):
        """默认提示词包含技能标识、Jira 链接与额外输入。"""
        prompt = build_skill_prompt(
            self.make_skill(),
            {"review_round": 2},
            "https://jira.example.com/browse/CALL-1",
        )

        self.assertIn("jira-gate-1", prompt)
        self.assertIn("https://jira.example.com/browse/CALL-1", prompt)
        self.assertIn("review_round", prompt)
        self.assertIn(".skill-result.json", prompt)

    def test_custom_template_keeps_unknown_placeholders(self):
        """自定义模板中的未知占位符保持原样，不会导致任务失败。"""
        prompt = build_skill_prompt(
            self.make_skill("技能 {skill_id} 处理 {jira_url}，未知 {unknown_key}"),
            {},
            "https://jira.example.com/browse/CALL-2",
        )

        self.assertTrue(
            prompt.startswith("技能 jira-gate-1 处理 https://jira.example.com/browse/CALL-2，未知 {unknown_key}")
        )
        self.assertIn(".skill-result.json", prompt)
        self.assertIn("不写入 Jira", prompt)

    def test_result_contract_is_not_appended_twice(self):
        """技能模板已包含结果文件约定时，不再重复追加。"""
        prompt = build_skill_prompt(
            self.make_skill("完成后写 .skill-result.json"),
            {},
            "https://jira.example.com/browse/CALL-3",
        )

        self.assertEqual(prompt.count(".skill-result.json"), 1)


class CodexCommandTests(unittest.TestCase):
    def test_command_carries_provider_settings_without_secret(self):
        """命令行包含 provider 覆盖参数，且不出现任何密钥明文。"""
        command = build_codex_command(
            codex_bin="codex",
            workspace_dir="/data/skill-workspace/demo/T1",
            prompt="执行技能",
            last_message_path="/tmp/last.md",
            model="codex/deepseek-flash",
            model_provider="skillrun",
            provider_base_url="https://newapi.example.com/v1",
            provider_wire_api="responses",
            provider_env_key="CODEX_SKILL_API_KEY",
            reasoning_effort="high",
            extra_args=["--add-dir", "/data/skills/demo"],
        )

        self.assertEqual(command[1], "exec")
        self.assertIn("--skip-git-repo-check", command)
        self.assertIn("--ephemeral", command)
        self.assertIn("model_providers.skillrun.base_url=https://newapi.example.com/v1", command)
        self.assertIn("model_providers.skillrun.env_key=CODEX_SKILL_API_KEY", command)
        self.assertEqual(command[-1], "执行技能")
        self.assertNotIn("sk-secret", " ".join(command))

    def test_env_only_exposes_configured_api_key(self):
        """密钥通过环境变量注入，并保留原有环境变量。"""
        env = build_codex_env({"PATH": "/usr/bin"}, api_key="sk-secret", api_key_env="MY_KEY", codex_home="/data/codex")

        self.assertEqual(env["MY_KEY"], "sk-secret")
        self.assertEqual(env["CODEX_HOME"], "/data/codex")
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_redact_command_hides_secret_values(self):
        """命令行回显时把密钥替换为占位符。"""
        redacted = redact_command(["codex", "-c", "token=sk-secret"], secrets=["sk-secret"])

        self.assertEqual(redacted[2], "token=***")

    def test_normalize_base_url_appends_v1_once(self):
        """模型中转地址统一补齐 /v1，重复调用不产生重复路径。"""
        self.assertEqual(normalize_base_url("https://api.example.com"), "https://api.example.com/v1")
        self.assertEqual(normalize_base_url("https://api.example.com/v1/"), "https://api.example.com/v1")
        self.assertEqual(normalize_base_url("", "https://fallback.example.com"), "https://fallback.example.com/v1")


class CodexEventTests(unittest.TestCase):
    def test_summarize_agent_message_and_command(self):
        """事件解析能识别模型消息、命令执行与会话信息。"""
        message = summarize_event({"type": "item.completed", "item": {"type": "agent_message", "text": "结论"}})
        command = summarize_event(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "node jira-cli.mjs get CALL-1", "exit_code": 0},
            }
        )
        session = summarize_event({"type": "thread.started", "thread_id": "T-1"})

        self.assertEqual(message.kind, "message")
        self.assertEqual(message.text, "结论")
        self.assertEqual(command.kind, "command")
        self.assertIn("node jira-cli.mjs", command.text)
        self.assertEqual(session.kind, "session")
        self.assertIn("T-1", session.text)

    def test_summarize_item_error_as_error_event(self):
        """item 级错误事件也被识别为 error，便于进度里直接看到上游报错。"""
        event = summarize_event(
            {"type": "item.completed", "item": {"type": "error", "message": "upstream 500"}}
        )

        self.assertEqual(event.kind, "error")
        self.assertIn("upstream 500", event.text)


class NetworkErrorDetectionTests(unittest.TestCase):
    def test_default_retry_limit_allows_slow_network_recovery(self):
        """默认阈值放宽到 10 次，给内网抖动与代理重连留出恢复时间。"""
        from app.skillrun import codex_runner

        self.assertEqual(codex_runner.DEFAULT_NETWORK_RETRY_LIMIT, 10)

    def test_network_markers_are_detected(self):
        """Codex 重连提示与常见网络错误都判定为网络类问题。"""
        self.assertTrue(is_network_error("Reconnecting... waiting for network (Connection failed: error sending request)"))
        self.assertTrue(is_network_error("dial tcp: connection refused"))
        self.assertFalse(is_network_error("Model metadata not found"))
        self.assertFalse(is_network_error(""))

    def test_failure_message_points_to_actionable_checks(self):
        """网络失败说明包含排查方向与原始报错，避免只给一个笼统错误。"""
        message = build_network_failure_message(3, "Connection failed: error sending request")

        self.assertIn("3 次网络错误", message)
        self.assertIn("CODEX_BASE_URL", message)
        self.assertIn("Connection failed", message)


class CodexProcessTests(unittest.TestCase):
    def make_workspace(self):
        """创建临时工作目录，返回目录路径。"""
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return temp_dir.name

    def test_run_collects_events_and_last_message(self):
        """子进程输出的 JSONL 事件与最终回复文件都能被正确收集。"""
        workspace = self.make_workspace()
        last_message_path = os.path.join(workspace, "last.md")
        script = (
            "import json\n"
            "print(json.dumps({'type': 'thread.started', 'thread_id': 'T-9'}), flush=True)\n"
            "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '中间结论'}}), flush=True)\n"
            "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 12}}), flush=True)\n"
            f"open({last_message_path!r}, 'w', encoding='utf-8').write('最终结论')\n"
        )

        result = run_codex_skill(
            [sys.executable, "-c", script],
            cwd=workspace,
            env=dict(os.environ),
            timeout_seconds=60,
            last_message_path=last_message_path,
        )

        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.session_id, "T-9")
        self.assertEqual(result.last_message, "最终结论")
        self.assertEqual(result.usage, {"input_tokens": 12})
        self.assertIn("message", [event["kind"] for event in result.events])

    def test_run_reports_non_zero_exit_code(self):
        """子进程非零退出时记录退出码与错误信息。"""
        workspace = self.make_workspace()

        result = run_codex_skill(
            [sys.executable, "-c", "import sys; sys.exit(3)"],
            cwd=workspace,
            env=dict(os.environ),
            timeout_seconds=60,
            last_message_path=os.path.join(workspace, "missing.md"),
        )

        self.assertEqual(result.exit_code, 3)
        self.assertIn("3", result.error or "")

    def test_run_terminates_process_when_timeout_exceeded(self):
        """超过超时时间时终止子进程并标记超时。"""
        workspace = self.make_workspace()

        result = run_codex_skill(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=workspace,
            env=dict(os.environ),
            timeout_seconds=2,
            last_message_path=os.path.join(workspace, "missing.md"),
        )

        self.assertTrue(result.timed_out)
        self.assertLess(result.duration_ms, 30000)

    def test_run_reports_start_failure(self):
        """无法启动 Codex 进程时返回可读错误而不是抛异常。"""
        workspace = self.make_workspace()

        result = run_codex_skill(
            ["definitely-not-a-real-binary-xyz"],
            cwd=workspace,
            env=dict(os.environ),
            timeout_seconds=10,
            last_message_path=os.path.join(workspace, "missing.md"),
        )

        self.assertIsNone(result.exit_code)
        self.assertIn("无法启动", result.error or "")

    def test_run_aborts_early_after_consecutive_network_errors(self):
        """连续网络错误时提前终止子进程，不再空转到超时。"""
        workspace = self.make_workspace()
        reconnect = (
            "Reconnecting... waiting for network "
            "(Connection failed: error sending request)"
        )
        script = (
            "import json, time\n"
            f"message = {reconnect!r}\n"
            "for _ in range(6):\n"
            "    print(json.dumps({'type': 'error', 'message': message}), flush=True)\n"
            "    time.sleep(0.2)\n"
            "time.sleep(60)\n"
        )

        result = run_codex_skill(
            [sys.executable, "-c", script],
            cwd=workspace,
            env=dict(os.environ),
            timeout_seconds=120,
            last_message_path=os.path.join(workspace, "missing.md"),
            network_retry_limit=3,
        )

        self.assertTrue(result.network_failed)
        self.assertFalse(result.timed_out)
        self.assertIn("网络错误", result.error or "")
        self.assertLess(result.duration_ms, 60000)

    def test_run_keeps_going_when_network_errors_are_below_limit(self):
        """低于阈值的网络抖动不终止任务，正常完成仍算成功。"""
        workspace = self.make_workspace()
        script = (
            "import json\n"
            "message = 'Reconnecting... waiting for network (Connection failed)'\n"
            "print(json.dumps({'type': 'error', 'message': message}), flush=True)\n"
            "print(json.dumps({'type': 'error', 'message': message}), flush=True)\n"
            "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '结论'}}), flush=True)\n"
            "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 5}}), flush=True)\n"
        )

        result = run_codex_skill(
            [sys.executable, "-c", script],
            cwd=workspace,
            env=dict(os.environ),
            timeout_seconds=60,
            last_message_path=os.path.join(workspace, "missing.md"),
            network_retry_limit=3,
        )

        self.assertFalse(result.network_failed)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.last_message, "结论")

    def test_progress_event_resets_network_error_streak(self):
        """网络错误之间出现正常进度时重新计数，避免把抖动误判为不可达。"""
        workspace = self.make_workspace()
        script = (
            "import json, time\n"
            "message = 'Reconnecting... waiting for network (Connection failed)'\n"
            "for _ in range(3):\n"
            "    print(json.dumps({'type': 'error', 'message': message}), flush=True)\n"
            "    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '仍在推进'}}), flush=True)\n"
            "    time.sleep(0.2)\n"
            "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 5}}), flush=True)\n"
        )

        result = run_codex_skill(
            [sys.executable, "-c", script],
            cwd=workspace,
            env=dict(os.environ),
            timeout_seconds=60,
            last_message_path=os.path.join(workspace, "missing.md"),
            network_retry_limit=3,
        )

        self.assertFalse(result.network_failed)
