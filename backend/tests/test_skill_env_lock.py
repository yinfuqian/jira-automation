"""技能运行参数锁定测试：覆盖配置唯一来源、缺失拦截与子进程环境注入。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

try:
    from tests.skill_env_fixture import LOCKED_SKILL_ENV
except ModuleNotFoundError:  # 兼容以 tests 目录为根直接运行
    from skill_env_fixture import LOCKED_SKILL_ENV

PROJECT_ROOT = BACKEND_DIR.parent
SKILLS_DIR = PROJECT_ROOT / "skills"

# 一套完整的技能运行配置，等价于 .env 里已把 7 项参数填好；与其它技能用例共用同一份测试值。
LOCKED_CONFIG = dict(LOCKED_SKILL_ENV)


def create_test_app(**overrides):
    """构造使用内存数据库、且已注入完整技能运行配置的测试应用。"""
    from app import create_app

    config = {
        "TESTING": True,
        "AUTH_TEST_BYPASS": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SKILLS_DIR": str(SKILLS_DIR),
        "SKILL_WORKSPACE_DIR": tempfile.mkdtemp(prefix="skill-workspace-"),
        # 用例统一使用 jira.example.com 这类外部域名，这里显式关闭主机白名单校验。
        "SKILL_URL_ALLOWED_HOSTS": "",
    }
    config.update(LOCKED_CONFIG)
    config.update(overrides)
    return create_app(config_overrides=config)


class DotenvAndEnvLockUnitTests(unittest.TestCase):
    """dotenv 加载与 env_lock 纯函数行为。"""

    def test_dotenv_skips_blank_values(self):
        """空值不写入环境变量，避免根 .env 的空 JIRA_TOKEN 挤掉已有令牌。"""
        from app.config import load_dotenv_file

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base.env"
            base.write_text("JIRA_TOKEN=real-token\n", encoding="utf-8")
            blank = Path(tmp) / "blank.env"
            blank.write_text("JIRA_TOKEN=\nJIRA_BASE_URL=\n", encoding="utf-8")

            os.environ.pop("JIRA_TOKEN", None)
            os.environ.pop("JIRA_BASE_URL", None)
            self.addCleanup(os.environ.pop, "JIRA_TOKEN", None)
            self.addCleanup(os.environ.pop, "JIRA_BASE_URL", None)

            load_dotenv_file(base)
            load_dotenv_file(blank)

            self.assertEqual(os.environ.get("JIRA_TOKEN"), "real-token")
            self.assertNotIn("JIRA_BASE_URL", os.environ)

    def test_collect_locked_env_marks_lock_and_skips_blank(self):
        """注入子进程的参数带锁定标记；留空项不注入，交由后端按未配置处理。"""
        from app.skillrun import env_lock

        locked = env_lock.collect_locked_env(dict(LOCKED_CONFIG, DEVOPS_MCP_TOKEN=""))

        self.assertEqual(locked[env_lock.LOCK_FLAG_ENV], "1")
        self.assertEqual(locked["JIRA_TOKEN"], "jira-token-from-env")
        self.assertEqual(locked["CODEX_MODEL"], "codex/test-model")
        self.assertNotIn("DEVOPS_MCP_TOKEN", locked)

    def test_missing_env_keys_only_blocks_required_items(self):
        """只有必填项缺失才拦截；DEVOPS_MCP_TOKEN 留空等于关闭热更新。"""
        from app.skillrun import env_lock

        config = dict(LOCKED_CONFIG, JIRA_TOKEN="", DEVOPS_MCP_TOKEN="")
        self.assertEqual(env_lock.missing_env_keys(config), ["JIRA_TOKEN"])

        message = env_lock.build_missing_env_message(["JIRA_TOKEN"])
        self.assertIn("JIRA_TOKEN", message)
        # 提示必须指向唯一权威来源：仓库根目录的 .env。
        self.assertIn("根目录的 .env", message)

        self.assertEqual(env_lock.missing_env_keys(LOCKED_CONFIG), [])

    def test_build_codex_env_overrides_parent_values(self):
        """父进程或宿主环境里的同名变量会被配置值覆盖，并带上锁定标记。"""
        from app.skillrun import env_lock
        from app.skillrun.codex_runner import build_codex_env

        parent = {
            "JIRA_TOKEN": "stale-token",
            "JIRA_BASE_URL": "https://stale.example.com",
            "PATH": "/usr/bin",
        }
        env = build_codex_env(
            parent,
            api_key="sk-secret",
            api_key_env="MY_KEY",
            codex_home="/data/codex",
            locked_env=env_lock.collect_locked_env(dict(LOCKED_CONFIG)),
        )

        self.assertEqual(env["JIRA_TOKEN"], "jira-token-from-env")
        self.assertEqual(env["JIRA_BASE_URL"], "https://jira.example.com")
        self.assertEqual(env["CODEX_MODEL_PROVIDER"], "testprovider")
        self.assertEqual(env[env_lock.LOCK_FLAG_ENV], "1")
        self.assertEqual(env["MY_KEY"], "sk-secret")
        self.assertEqual(env["PATH"], "/usr/bin")
        # 传入的父环境字典不被就地修改
        self.assertEqual(parent["JIRA_TOKEN"], "stale-token")


class SubmitSkillRunEnvLockTests(unittest.TestCase):
    """提交接口在配置缺失时直接拒绝，而不是让技能自行回落到令牌文件。"""

    def setUp(self):
        """准备独立应用与内存表结构。"""
        from extensions import db

        self.app = create_test_app()
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.addCleanup(self._cleanup_context)
        self.client = self.app.test_client()

    def _cleanup_context(self):
        """退出应用上下文并释放连接。"""
        from extensions import db

        db.session.remove()
        self.context.pop()

    def test_submit_rejected_when_jira_token_missing(self):
        """缺 JIRA_TOKEN 时返回 503、带上缺失项，并且不落任何任务记录。"""
        from app.skillrun import store

        self.app.config["JIRA_TOKEN"] = ""
        response = self.client.post(
            "/skill/run",
            json={"skill_id": "jira-gate-1", "jira_url": "https://jira.example.com/browse/CALL-1"},
        )

        self.assertEqual(response.status_code, 503)
        payload = response.get_json()
        self.assertIn("JIRA_TOKEN", payload["missing_env"])
        self.assertEqual(store.list_records(), [])

    def test_submit_allowed_with_full_config(self):
        """配置齐全时正常入队。"""
        with patch("app.skillrun.routes.routes.run_skill_task.apply_async"):
            response = self.client.post(
                "/skill/run",
                json={"skill_id": "jira-gate-1", "jira_url": "https://jira.example.com/browse/CALL-1"},
            )

        self.assertEqual(response.status_code, 202)


class SkillTaskEnvLockTests(unittest.TestCase):
    """任务执行阶段的配置校验与子进程环境注入。"""

    def setUp(self):
        """准备独立应用、任务记录与工作目录。"""
        from app.skillrun import store
        from extensions import db

        self.app = create_test_app()
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.addCleanup(self._cleanup_context)
        self.record = store.create_record(
            skill_id="jira-gate-1",
            jira_url="https://jira.example.com/browse/CALL-1446",
            inputs={},
            requested_by="tester",
        )
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.app.config["SKILL_WORKSPACE_DIR"] = self.workspace.name

    def _cleanup_context(self):
        """退出应用上下文并释放连接。"""
        from extensions import db

        db.session.remove()
        self.context.pop()

    def test_task_fails_before_codex_when_config_missing(self):
        """缺 JIRA_TOKEN 时不启动 Codex，任务直接失败并记录中文原因。"""
        from app.skillrun.models.model import SkillRunRecord
        from app.skillrun.tasks import run_skill_task
        from extensions import db

        self.app.config["JIRA_TOKEN"] = ""

        with patch("app.skillrun.tasks.codex_runner.run_codex_skill") as runner:
            payload = run_skill_task.apply(args=[self.record.id]).get()

        self.assertFalse(runner.called)
        self.assertEqual(payload["status"], "failed")
        db.session.expire_all()
        record = db.session.get(SkillRunRecord, self.record.id)
        self.assertEqual(record.status, SkillRunRecord.STATUS_FAILED)
        self.assertEqual(record.stage, "env_not_configured")
        self.assertIn("JIRA_TOKEN", record.error_message)

    def test_task_injects_locked_env_and_flag(self):
        """执行前用配置值覆盖子进程环境，并写入锁定标记。"""
        from app.skillrun import store
        from app.skillrun.codex_runner import CodexRunResult
        from app.skillrun.tasks import run_skill_task

        self.record = store.create_record(
            skill_id="jira-code",
            jira_url="https://jira.example.com/browse/CALL-1446",
            inputs={},
            requested_by="tester",
        )
        with patch("app.skillrun.tasks.codex_runner.run_codex_skill") as runner:
            runner.return_value = CodexRunResult(exit_code=0, last_message="已完成", duration_ms=100)
            with patch.dict(
                os.environ,
                {"JIRA_TOKEN": "stale-token", "JIRA_BASE_URL": "https://stale.example.com"},
            ):
                run_skill_task.apply(args=[self.record.id]).get()
            env = runner.call_args.kwargs["env"]

        self.assertEqual(env["JIRA_TOKEN"], "jira-token-from-env")
        self.assertEqual(env["JIRA_BASE_URL"], "https://jira.example.com")
        self.assertEqual(env["CODEX_MODEL"], "codex/test-model")
        self.assertEqual(env["CODEX_MODEL_PROVIDER"], "testprovider")
        self.assertEqual(env["CODEX_BASE_URL"], "https://model.example.com/v1")
        self.assertEqual(env["DEVOPS_MCP_URL"], "https://devops.example.com/mcp")
        self.assertEqual(env["DEVOPS_MCP_TOKEN"], "devops-token-from-env")
        self.assertEqual(env["SKILLRUN_ENV_LOCKED"], "1")

    def test_non_pipeline_skill_does_not_receive_devops_credentials(self):
        """缺陷门禁等非流水线技能不注入 MCP 地址与令牌。"""
        from app.skillrun.codex_runner import CodexRunResult
        from app.skillrun.tasks import run_skill_task

        with patch("app.skillrun.tasks.codex_runner.run_codex_skill") as runner:
            runner.return_value = CodexRunResult(exit_code=0, last_message="已完成", duration_ms=100)
            run_skill_task.apply(args=[self.record.id]).get()
            env = runner.call_args.kwargs["env"]

        self.assertNotIn("DEVOPS_MCP_URL", env)
        self.assertNotIn("DEVOPS_MCP_TOKEN", env)
        self.assertEqual(env["JIRA_TOKEN"], "jira-token-from-env")


if __name__ == "__main__":
    unittest.main()
