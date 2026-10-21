"""技能任务测试：覆盖 Codex 成功、超时、失败、技能缺失与提示词内容。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

try:
    from tests.skill_env_fixture import with_locked_skill_env
except ModuleNotFoundError:  # 兼容以 tests 目录为根直接运行
    from skill_env_fixture import with_locked_skill_env

PROJECT_ROOT = BACKEND_DIR.parent
SKILLS_DIR = PROJECT_ROOT / "skills"


def create_test_app(**overrides):
    """构造使用内存数据库的测试应用。"""
    from app import create_app

    config = {
        "TESTING": True,
        "AUTH_TEST_BYPASS": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SKILLS_DIR": str(SKILLS_DIR),
        "SKILL_WORKSPACE_DIR": tempfile.gettempdir(),
    }
    # 技能运行参数取自测试夹具：缺配置时任务会被直接拒绝，用例不应依赖本机 .env。
    config = with_locked_skill_env(config)
    config.update(overrides)
    return create_app(config_overrides=config)


class SkillTaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """创建类级应用与内存表，保证 Celery 任务与测试共用同一应用上下文。"""
        from extensions import db

        cls.app = create_test_app()
        cls.context = cls.app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        """清理会话与内存数据库连接。"""
        from extensions import db

        db.session.remove()
        db.engine.dispose()
        cls.context.pop()

    def setUp(self):
        """清空任务表并准备一条排队记录。"""
        from app.skillrun import store
        from app.skillrun.models.model import SkillRunRecord
        from extensions import db

        db.session.query(SkillRunRecord).delete()
        db.session.commit()
        self.record = store.create_record(
            skill_id="jira-gate-1",
            jira_url="https://jira.in.wezhuiyi.com/browse/CALL-1446",
            inputs={"review_round": 2},
            requested_by="tester",
        )
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.app.config["SKILL_WORKSPACE_DIR"] = self.workspace.name

    def run_task(self, run_result=None, side_effect=None):
        """在 mock 掉 Codex 子进程的前提下同步执行技能任务。"""
        from app.skillrun.tasks import run_skill_task

        with patch("app.skillrun.tasks.codex_runner.run_codex_skill") as runner:
            if side_effect is not None:
                runner.side_effect = side_effect
            else:
                runner.return_value = run_result
            result = run_skill_task.apply(args=[self.record.id])
            payload = result.get()
            # 任务在独立的应用上下文中写库，这里刷新本地会话以读取最新状态。
            from extensions import db

            db.session.expire_all()
            return payload, runner

    def test_successful_run_persists_result_and_session(self):
        """Codex 正常结束时记录状态为成功并保存结论与统计信息。"""
        from app.skillrun import store
        from app.skillrun.codex_runner import CodexRunResult

        payload, _ = self.run_task(
            CodexRunResult(
                exit_code=0,
                last_message="结论：达标，难度中",
                session_id="session-1",
                duration_ms=1500,
                events=[{"kind": "message", "text": "结论", "at": 1.0}],
                usage={"input_tokens": 100},
            )
        )

        record = store.find_record(self.record.task_id)
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(record.status, "succeeded")
        self.assertEqual(record.result_text, "结论：达标，难度中")
        self.assertEqual(record.codex_session_id, "session-1")
        self.assertEqual(record.duration_ms, 1500)
        self.assertIsNone(record.error_message)
        self.assertIsNotNone(record.finished_at)

    def test_timeout_marks_record_as_timeout(self):
        """Codex 执行超时时记录状态为超时并保留错误说明。"""
        from app.skillrun import store
        from app.skillrun.codex_runner import CodexRunResult

        payload, _ = self.run_task(
            CodexRunResult(exit_code=None, timed_out=True, error="技能执行超过 1800 秒，已强制终止", duration_ms=1800000)
        )

        record = store.find_record(self.record.task_id)
        self.assertEqual(payload["status"], "timeout")
        self.assertEqual(record.status, "timeout")
        self.assertIn("已强制终止", record.error_message)

    def run_jira_code_task(self):
        """用 jira-code（runtime.json 声明 7200 秒）跑一次任务，返回 Codex mock 以读取实际超时参数。"""
        from app.skillrun import store
        from app.skillrun.codex_runner import CodexRunResult

        self.record = store.create_record(
            skill_id="jira-code",
            jira_url="https://jira.in.wezhuiyi.com/browse/CALL-1446",
            inputs={},
            requested_by="tester",
        )
        _, runner = self.run_task(CodexRunResult(exit_code=0, last_message="已完成", duration_ms=1000))
        return runner

    def test_timeout_follows_skill_declaration_when_not_configured(self):
        """未配置 CODEX_SKILL_TIMEOUT 时按技能自身声明执行：jira-code 拿到 7200 秒，而不是全局默认 1800。"""
        original = self.app.config.pop("CODEX_SKILL_TIMEOUT", None)
        try:
            runner = self.run_jira_code_task()
            self.assertEqual(runner.call_args.kwargs["timeout_seconds"], 7200)
        finally:
            if original is not None:
                self.app.config["CODEX_SKILL_TIMEOUT"] = original

    def test_timeout_config_overrides_skill_declaration(self):
        """显式配置 CODEX_SKILL_TIMEOUT 时以配置为准，便于运维统一收紧或放宽所有技能。"""
        original = self.app.config.get("CODEX_SKILL_TIMEOUT")
        self.app.config["CODEX_SKILL_TIMEOUT"] = 900
        try:
            runner = self.run_jira_code_task()
            self.assertEqual(runner.call_args.kwargs["timeout_seconds"], 900)
        finally:
            if original is None:
                self.app.config.pop("CODEX_SKILL_TIMEOUT", None)
            else:
                self.app.config["CODEX_SKILL_TIMEOUT"] = original

    def test_registers_devops_mcp_config_before_running(self):
        """jira-code 需要流水线 MCP：配置写进任务独立 CODEX_HOME 的 config.toml。"""
        import tomllib

        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.app.config["CODEX_HOME"] = home.name
        self.app.config["DEVOPS_MCP_URL"] = "https://devops.ks1.wezhuiyi.com/mcp"
        self.app.config["DEVOPS_MCP_TOKEN"] = "token-for-test-1234"
        self.addCleanup(self.app.config.pop, "DEVOPS_MCP_TOKEN", None)

        self.run_jira_code_task()

        task_home = Path(home.name) / "runs" / self.record.task_id
        document = tomllib.loads((task_home / "config.toml").read_text(encoding="utf-8"))
        server = document["mcp_servers"]["pipeline-integration-mcp"]
        self.assertEqual(server["url"], "https://devops.ks1.wezhuiyi.com/mcp")
        self.assertEqual(server["http_headers"]["X-User-Tokens"], "token-for-test-1234")

    def test_skips_devops_mcp_for_unrelated_skill(self):
        """缺陷门禁等技能不应注册流水线 MCP，避免暴露与任务无关的工具。"""
        from app.skillrun.codex_runner import CodexRunResult

        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.app.config["CODEX_HOME"] = home.name
        self.app.config["DEVOPS_MCP_URL"] = "https://devops.ks1.wezhuiyi.com/mcp"
        self.app.config["DEVOPS_MCP_TOKEN"] = "token-for-test-1234"
        self.addCleanup(self.app.config.pop, "DEVOPS_MCP_TOKEN", None)

        self.run_task(CodexRunResult(exit_code=0, last_message="已完成", duration_ms=100))

        task_home = Path(home.name) / "runs" / self.record.task_id
        self.assertFalse((task_home / "config.toml").exists())

    def test_no_mcp_config_when_token_absent(self):
        """没配令牌时不写 MCP 配置，免得留下一个连不通的 server。"""
        from app.skillrun.codex_runner import CodexRunResult

        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.app.config["CODEX_HOME"] = home.name
        self.app.config.pop("DEVOPS_MCP_TOKEN", None)

        self.run_task(CodexRunResult(exit_code=0, last_message="已完成", duration_ms=100))

        self.assertFalse((Path(home.name) / "config.toml").exists())
        self.assertFalse((Path(home.name) / "runs" / self.record.task_id / "config.toml").exists())

    def test_each_task_uses_isolated_codex_home_without_global_skills(self):
        """共享 CODEX_HOME 里的 skills/ 不能进入任务目录，避免 Codex 自动发现并误调用其它技能。"""
        from app.skillrun.codex_runner import CodexRunResult

        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.app.config["CODEX_HOME"] = home.name
        global_skills = Path(home.name) / "skills" / "jira-code"
        global_skills.mkdir(parents=True)
        (global_skills / "SKILL.md").write_text("---\nname: jira-code\n---\n", encoding="utf-8")

        _, runner = self.run_task(CodexRunResult(exit_code=0, last_message="已完成", duration_ms=100))

        env = runner.call_args.kwargs["env"]
        task_home = Path(env["CODEX_HOME"])
        self.assertEqual(task_home, Path(home.name) / "runs" / self.record.task_id)
        self.assertFalse((task_home / "skills").exists())

    def test_non_zero_exit_marks_record_as_failed(self):
        """Codex 非零退出时记录状态为失败。"""
        from app.skillrun import store
        from app.skillrun.codex_runner import CodexRunResult

        payload, _ = self.run_task(CodexRunResult(exit_code=2, error="Codex 退出码为 2", duration_ms=800))

        record = store.find_record(self.record.task_id)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.exit_code, 2)

    def test_success_without_final_message_is_failed(self):
        """流程成功但没有最终结论时视为失败，避免误报达标。"""
        from app.skillrun import store
        from app.skillrun.codex_runner import CodexRunResult

        payload, _ = self.run_task(CodexRunResult(exit_code=0, last_message="", duration_ms=100))

        record = store.find_record(self.record.task_id)
        self.assertEqual(payload["status"], "failed")
        self.assertIn("未返回最终结论", record.error_message)

    def test_missing_skill_marks_record_failed_without_calling_codex(self):
        """技能不存在时不调用 Codex，直接记录失败原因。"""
        from app.skillrun import store

        self.record.skill_id = "not-installed"
        from extensions import db

        db.session.add(self.record)
        db.session.commit()

        payload, runner = self.run_task()

        self.assertEqual(payload["status"], "failed")
        self.assertEqual(store.find_record(self.record.task_id).status, "failed")
        runner.assert_not_called()

    def test_prompt_and_command_include_skill_jira_url_and_workspace(self):
        """任务把技能目录、Jira 链接与独立工作目录传给 Codex。"""
        from app.skillrun.codex_runner import CodexRunResult

        _, runner = self.run_task(CodexRunResult(exit_code=0, last_message="结论", duration_ms=10))

        call_kwargs = runner.call_args.kwargs
        command = runner.call_args.args[0]
        self.assertIn("jira.in.wezhuiyi.com/browse/CALL-1446", command[-1])
        self.assertIn("jira-gate-1", command[-1])
        self.assertIn(self.record.task_id, call_kwargs["cwd"])
        self.assertIn("--add-dir", command)
        self.assertIn(str(SKILLS_DIR / "jira-gate-1"), command)

    def test_unexpected_exception_is_recorded_as_failure(self):
        """Codex 调用抛出异常时记录失败，不让任务静默丢失。"""
        from app.skillrun import store

        payload, _ = self.run_task(side_effect=RuntimeError("codex 未安装"))

        record = store.find_record(self.record.task_id)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(record.status, "failed")
        self.assertIn("codex 未安装", record.error_message)

    def test_network_failure_uses_dedicated_stage(self):
        """网络不可达时单独标记阶段，便于与业务失败区分并快速定位。"""
        from app.skillrun import store
        from app.skillrun.codex_runner import CodexRunResult

        payload, _ = self.run_task(
            CodexRunResult(
                exit_code=1,
                network_failed=True,
                error="Codex 连续 3 次网络错误，无法访问模型中转或 Jira，已提前终止本次任务",
                duration_ms=25000,
            )
        )

        record = store.find_record(self.record.task_id)
        self.assertEqual(payload["status"], "failed")
        self.assertTrue(payload["network_failed"])
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.stage, "network_unreachable")
        self.assertIn("网络错误", record.error_message)

    def test_network_retry_limit_is_passed_from_config(self):
        """网络错误阈值从配置传入 Codex 执行器，便于按环境调整。"""
        from app.skillrun.codex_runner import CodexRunResult

        self.app.config["CODEX_NETWORK_RETRY_LIMIT"] = 5
        _, runner = self.run_task(CodexRunResult(exit_code=0, last_message="结论", duration_ms=10))

        self.assertEqual(runner.call_args.kwargs["network_retry_limit"], 5)
