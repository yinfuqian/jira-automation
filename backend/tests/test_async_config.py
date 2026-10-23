import importlib.util
import os
import sys
import unittest
import tempfile
from pathlib import Path
from urllib.parse import urlparse, unquote
from unittest.mock import patch


CONFIG_PATH = Path(__file__).resolve().parents[1] / "app" / "config.py"


def load_config_module():
    spec = importlib.util.spec_from_file_location("config_under_test", CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["config_under_test"] = module
    spec.loader.exec_module(module)
    return module


class AsyncConfigTests(unittest.TestCase):
    def test_analysis_model_key_falls_back_to_codex_api_key(self):
        module = load_config_module()
        env = {
            "OPENAI_KEY": "",
            "OPENAI_API_KEY": "",
            "CODEX_API_KEY": "codex-key",
        }
        with patch.dict(os.environ, env, clear=False):
            resolved = module._resolve_openai_api_key()

        self.assertEqual(resolved, "codex-key")

    def test_analysis_model_key_prefers_explicit_openai_key(self):
        module = load_config_module()
        env = {
            "OPENAI_KEY": "analysis-key",
            "OPENAI_API_KEY": "standard-key",
            "CODEX_API_KEY": "codex-key",
        }
        with patch.dict(os.environ, env, clear=False):
            resolved = module._resolve_openai_api_key()

        self.assertEqual(resolved, "analysis-key")

    def test_fault_analysis_model_and_reasoning_effort_are_environment_driven(self):
        env = {
            "OPENAI_MODEL": "gpt-5.6-sol",
            "OPENAI_REASONING_EFFORT": "high",
        }
        with patch.dict(os.environ, env, clear=False):
            config = load_config_module().Config

        self.assertEqual(config.OPENAI_MODEL, "gpt-5.6-sol")
        self.assertEqual(config.OPENAI_REASONING_EFFORT, "high")

    def test_fault_analysis_model_defaults_are_declared_in_config_source(self):
        source = CONFIG_PATH.read_text(encoding="utf-8")

        self.assertIn('os.getenv("OPENAI_MODEL", "gpt-5.6-sol")', source)
        self.assertIn('os.getenv("OPENAI_REASONING_EFFORT", "high")', source)

    def test_analysis_loopback_trust_defaults_to_enabled_and_can_be_disabled(self):
        """同机分析调用默认视为内部服务调用；需要强制令牌时仍可显式关闭。"""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANALYSIS_API_TRUST_LOOPBACK", None)
            enabled = load_config_module().Config.ANALYSIS_API_TRUST_LOOPBACK
        with patch.dict(os.environ, {"ANALYSIS_API_TRUST_LOOPBACK": "false"}, clear=False):
            disabled = load_config_module().Config.ANALYSIS_API_TRUST_LOOPBACK

        self.assertTrue(enabled)
        self.assertFalse(disabled)

    def test_database_defaults_to_requested_mysql_database(self):
        env = {
            "MYSQL_HOST": "180.184.70.137",
            "MYSQL_PORT": "3306",
            "MYSQL_DATABASE": "jira_automation",
            "MYSQL_USERNAME": "easygo",
            "MYSQL_PASSWORD": "secret-password",
        }
        with patch.dict(os.environ, env, clear=False):
            config = load_config_module().Config
        parsed = urlparse(config.SQLALCHEMY_DATABASE_URI)

        self.assertEqual(parsed.hostname, "180.184.70.137")
        self.assertEqual(parsed.port, 3306)
        self.assertEqual(parsed.path.lstrip("/"), "jira_automation")
        self.assertEqual(parsed.username, "easygo")
        self.assertEqual(unquote(parsed.password), "secret-password")

    def test_redis_defaults_to_requested_async_broker(self):
        env = {
            "REDIS_HOST": "180.184.70.137",
            "REDIS_PORT": "16379",
            "REDIS_DATABASE": "0",
            "REDIS_USERNAME": "default",
            "REDIS_PASSWORD": "secret-redis-password",
        }
        with patch.dict(os.environ, env, clear=False):
            config = load_config_module().Config
        parsed = urlparse(config.REDIS_URL)

        self.assertEqual(parsed.hostname, "180.184.70.137")
        self.assertEqual(parsed.port, 16379)
        self.assertEqual(parsed.path, "/0")
        self.assertEqual(parsed.username, "default")
        self.assertEqual(parsed.password, "secret-redis-password")
        self.assertEqual(config.CELERY_BROKER_URL, config.REDIS_URL)
        self.assertEqual(config.CELERY_RESULT_BACKEND, config.REDIS_URL)

    def test_celery_redis_transport_enables_resilient_connections(self):
        config = load_config_module().Config

        self.assertTrue(config.CELERY_BROKER_TRANSPORT_OPTIONS["socket_keepalive"])
        self.assertTrue(config.CELERY_BROKER_TRANSPORT_OPTIONS["retry_on_timeout"])
        self.assertGreaterEqual(config.CELERY_BROKER_TRANSPORT_OPTIONS["health_check_interval"], 30)
        self.assertGreaterEqual(config.CELERY_BROKER_TRANSPORT_OPTIONS["socket_connect_timeout"], 5)
        self.assertGreaterEqual(config.CELERY_BROKER_TRANSPORT_OPTIONS["socket_timeout"], 300)
        self.assertGreaterEqual(config.CELERY_BROKER_TRANSPORT_OPTIONS["visibility_timeout"], 3600)
        self.assertGreaterEqual(config.CELERY_BROKER_POOL_LIMIT, 4)
        self.assertTrue(config.CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS["retry_on_timeout"])
        self.assertGreaterEqual(config.CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS["socket_connect_timeout"], 5)
        self.assertGreaterEqual(config.CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS["socket_timeout"], 30)

    def test_celery_broker_timeout_is_independent_from_short_redis_client_timeout(self):
        env = {
            "REDIS_SOCKET_TIMEOUT": "60",
            "CELERY_BROKER_SOCKET_TIMEOUT": "360",
        }
        with patch.dict(os.environ, env, clear=False):
            config = load_config_module().Config

        self.assertEqual(config.REDIS_SOCKET_TIMEOUT, 60)
        self.assertEqual(config.CELERY_BROKER_TRANSPORT_OPTIONS["socket_timeout"], 360)
        self.assertEqual(config.CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS["socket_timeout"], 60)

    def test_database_engine_uses_pre_ping_for_remote_mysql(self):
        config = load_config_module().Config

        self.assertTrue(config.SQLALCHEMY_ENGINE_OPTIONS["pool_pre_ping"])
        self.assertGreater(config.SQLALCHEMY_ENGINE_OPTIONS["pool_recycle"], 0)

    def test_authentication_settings_are_environment_driven(self):
        env = {
            "AUTH_USERS_FILE": "C:/secure/users.json",
            "AUTH_LOGIN_MAX_FAILURES": "7",
            "AUTH_LOGIN_WINDOW_SECONDS": "420",
        }
        with patch.dict(os.environ, env, clear=False):
            config = load_config_module().Config

        self.assertEqual(config.AUTH_USERS_FILE, "C:/secure/users.json")
        self.assertEqual(config.AUTH_LOGIN_MAX_FAILURES, 7)
        self.assertEqual(config.AUTH_LOGIN_WINDOW_SECONDS, 420)

    def test_default_auth_users_file_is_the_project_users_csv(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUTH_USERS_FILE", None)
            config = load_config_module().Config

        project_root = Path(__file__).resolve().parents[2]
        self.assertEqual(Path(config.AUTH_USERS_FILE), project_root / "users.csv")
        self.assertTrue(Path(config.AUTH_USERS_FILE).exists())

    def test_relative_auth_users_file_is_resolved_from_backend_directory(self):
        with patch.dict(os.environ, {"AUTH_USERS_FILE": "../users.csv"}, clear=False):
            config = load_config_module().Config

        project_root = Path(__file__).resolve().parents[2]
        self.assertEqual(Path(config.AUTH_USERS_FILE), project_root / "users.csv")

    def test_dotenv_loader_sets_missing_values_without_overriding_existing_env(self):
        module = load_config_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                "MYSQL_HOST=180.184.70.137\n"
                "MYSQL_USERNAME=from-file\n"
                "QUOTED_VALUE=\"hello world\"\n"
                "EMPTY_VALUE=\n"
                "# comment should be ignored\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"MYSQL_USERNAME": "from-os"}, clear=False):
                os.environ.pop("MYSQL_HOST", None)
                os.environ.pop("QUOTED_VALUE", None)
                module.load_dotenv_file(env_file)

                self.assertEqual(os.environ["MYSQL_HOST"], "180.184.70.137")
                self.assertEqual(os.environ["MYSQL_USERNAME"], "from-os")
                self.assertEqual(os.environ["QUOTED_VALUE"], "hello world")
                # 空值表示「未配置」：不再写入环境变量，避免根 .env 里的空键挤掉
                # backend/.env 中已填好的同名有效值。
                self.assertNotIn("EMPTY_VALUE", os.environ)

    def test_dotenv_loader_uses_last_value_for_duplicate_keys_in_same_file(self):
        module = load_config_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                "OPENAI_URL=https://api.deepseek.com\n"
                "OPENAI_URL=https://newapi.in.wezhuiyi.com/v1\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                module.load_dotenv_file(env_file)

                self.assertEqual(os.environ["OPENAI_URL"], "https://newapi.in.wezhuiyi.com/v1")

    def test_load_local_env_treats_root_env_as_authority(self):
        """先读 backend/.env 兜底、再用根 .env 覆盖：根 .env 是唯一权威来源。"""
        module = load_config_module()
        calls = []
        original = module.load_dotenv_file

        def recorder(path, override=False, protected_keys=None):
            """记录加载顺序与覆盖标记，不真正写环境变量。"""
            calls.append((Path(path).parent.name, Path(path).name, override, bool(protected_keys)))

        module.load_dotenv_file = recorder
        try:
            module.load_local_env()
        finally:
            module.load_dotenv_file = original

        # 用真实目录名比较，改工程名（重命名仓库目录）后这条断言不需要跟着改。
        project_root_name = CONFIG_PATH.resolve().parents[2].name
        self.assertEqual(
            [(item[0], item[1]) for item in calls],
            [("backend", ".env"), (project_root_name, ".env")],
        )
        self.assertFalse(calls[0][2])
        self.assertTrue(calls[1][2])
        # 两个文件都要保护真实进程环境变量，避免 .env 覆盖 docker compose 注入的值。
        self.assertTrue(all(item[3] for item in calls))

    def test_dotenv_loader_keeps_protected_process_values(self):
        """protected_keys 中的变量来自真实进程环境，override=True 也不得改写。"""
        module = load_config_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text("JIRA_BASE_URL=https://from-file.example.com\n", encoding="utf-8")

            with patch.dict(os.environ, {"JIRA_BASE_URL": "https://from-process.example.com"}, clear=False):
                module.load_dotenv_file(env_file, override=True, protected_keys={"JIRA_BASE_URL"})

                self.assertEqual(os.environ["JIRA_BASE_URL"], "https://from-process.example.com")


if __name__ == "__main__":
    unittest.main()
