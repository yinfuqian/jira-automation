import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEV_COMPOSE_PATH = PROJECT_ROOT / "docker-compose-dev.yaml"
PROD_COMPOSE_PATH = PROJECT_ROOT / "docker-compose-prod.yaml"


class ComposeContractTests(unittest.TestCase):
    def test_online_compose_declares_expected_service_graph(self):
        source = DEV_COMPOSE_PATH.read_text(encoding="utf-8")

        for service in (
            "api-dev",
            "worker-analysis-dev",
            "worker-skill-dev",
            "worker-chatbot-dev",
            "chatbot-gateway-dev",
            "beat-dev",
            "migrate",
        ):
            self.assertRegex(source, rf"(?m)^  {re.escape(service)}:\s*$")
        self.assertNotRegex(source, r"(?m)^  (mysql|redis|frontend):\s*$")
        self.assertIn("      - api-dev", source)
        self.assertIn("      - worker-analysis-dev", source)
        self.assertIn("      - worker-skill-dev", source)

    def test_online_compose_uses_prebuilt_backend_image_and_host_network(self):
        source = DEV_COMPOSE_PATH.read_text(encoding="utf-8")

        self.assertNotIn("build:", source)
        self.assertEqual(source.count("image: ${BACKEND_IMAGE}"), 7)
        self.assertEqual(source.count('network_mode: "host"'), 7)
        self.assertIn("0.0.0.0:6000", source)
        self.assertIn('"${API_PORT}:6000"', source)

    def test_split_workers_have_unique_celery_node_names(self):
        """host 网络下多个 Worker 共用主机名，必须显式设置唯一节点名供巡检识别。"""
        expected = {
            DEV_COMPOSE_PATH: (
                "worker-analysis-dev@%h",
                "worker-skill-dev@%h",
                "worker-chatbot-dev@%h",
            ),
            PROD_COMPOSE_PATH: (
                "worker-analysis@%h",
                "worker-skill@%h",
                "worker-chatbot@%h",
            ),
        }
        for path, hostnames in expected.items():
            source = path.read_text(encoding="utf-8")
            for hostname in hostnames:
                self.assertIn(f"--hostname={hostname}", source)

    def test_online_compose_mounts_runtime_data_and_skills(self):
        source = DEV_COMPOSE_PATH.read_text(encoding="utf-8")

        self.assertEqual(source.count("env_file:\n      - .env"), 7)
        self.assertEqual(source.count("./data/users-dev.csv:/data/users.csv:ro"), 7)
        self.assertNotIn(":/users.csv", source)
        for mount in (
            "./data/upload-dev:/data/upload",
            "./data/logs-dev:/app/app/logs",
            "./data/repos-dev:/tmp/log-analyzer-repos",
            "./skills:/data/skills:ro",
            "./data/codex-dev:/data/codex",
            "./data/celerybeat-dev:/data/celerybeat",
            "./data/skill-workspace-dev:/data/skill-workspace",
            "./data/m2-dev/repository:/root/.m2/repository",
        ):
            self.assertIn(mount, source)
        for production_mount in (
            "./data/upload:/data/upload",
            "./data/logs:/app/app/logs",
            "./data/repos:/tmp/log-analyzer-repos",
            "./data/codex:/data/codex",
            "./data/users.csv:/data/users.csv:ro",
            "./data/skill-workspace:/data/skill-workspace",
            "./data/m2/repository:/root/.m2/repository",
        ):
            self.assertNotIn(production_mount, source)

    def test_env_example_has_unique_runtime_defaults(self):
        """示例配置不能用重复键意外覆盖运行镜像默认值。"""
        source = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        keys = [
            line.split("=", 1)[0].strip()
            for line in source.splitlines()
            if line.strip() and not line.lstrip().startswith("#") and "=" in line
        ]

        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn("BACKEND_IMAGE=jira-automation-backend:latest", source)
        self.assertIn("MYSQL_HOST=127.0.0.1", source)
        self.assertIn("REDIS_HOST=127.0.0.1", source)
        self.assertIn("API_PORT=6000", source)
        self.assertNotIn("CELERY_ANALYSIS_CONCURRENCY", source)
        self.assertNotIn("CELERY_SKILL_CONCURRENCY", source)
        # 机器人只把凭证放环境变量，项目与分支映射必须留在配置文件。
        self.assertIn("CHATBOT_BOT_ID=", source)
        self.assertNotIn("CHATBOT_PROJECT_", source)

    def test_chatbot_config_is_mounted_as_a_file_backed_contract(self):
        """API、机器人 Worker 和网关都要能读取同一份项目映射文件。"""
        config_path = PROJECT_ROOT / "config" / "chatbot.json"
        self.assertTrue(config_path.is_file())
        for path in (DEV_COMPOSE_PATH, PROD_COMPOSE_PATH):
            source = path.read_text(encoding="utf-8")
            self.assertEqual(source.count("./config:/data/config:ro"), 3)

    def test_start_script_selects_services_by_deploy_env(self):
        """启动脚本按 DEPLOY_ENV 选择生产或开发的服务名，并包含机器人服务。"""
        source = (PROJECT_ROOT / "start.sh").read_text(encoding="utf-8")
        self.assertNotIn("run --rm migrate", source)
        self.assertIn("DEPLOY_ENV", source)
        self.assertIn("docker-compose-dev.yaml", source)
        self.assertIn("docker-compose-prod.yaml", source)
        self.assertIn("worker-chatbot", source)
        self.assertIn("chatbot-gateway", source)
        # ps 不接受 up 专用参数，避免脚本在查询状态时直接失败。
        ps_line = next(line for line in source.splitlines() if " compose " in line and " ps " in line)
        self.assertNotIn("--force-recreate", ps_line)
        self.assertNotIn("--remove-orphans", ps_line)

    def test_production_restart_script_uses_non_dev_chatbot_services(self):
        """生产脚本必须使用不带 -dev 后缀的服务名，不能引用开发容器。"""
        source = (PROJECT_ROOT / "restart-master.sh").read_text(encoding="utf-8")
        self.assertIn("SERVICES+=(worker-chatbot chatbot-gateway)", source)
        self.assertNotIn("worker-chatbot-dev", source)
        self.assertNotIn("chatbot-gateway-dev", source)

    def test_readme_uses_online_compose_and_environment_specific_restart_scripts(self):
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

        dev_source = DEV_COMPOSE_PATH.read_text(encoding="utf-8")
        prod_source = PROD_COMPOSE_PATH.read_text(encoding="utf-8")
        self.assertIn("api-dev:", dev_source)
        self.assertIn("worker-analysis-dev:", dev_source)
        self.assertIn("worker-skill-dev:", dev_source)
        self.assertIn("beat-dev:", dev_source)
        self.assertIn("api:", prod_source)
        self.assertIn("beat:", prod_source)
        self.assertIn("docker-compose-dev.yaml", readme)
        self.assertIn("docker-compose-prod.yaml", readme)
        self.assertIn("restart-dev.sh", readme)
        self.assertIn("restart-master.sh", readme)
        self.assertIn("restart-dev.sh api-dev", readme)
        self.assertIn("http://127.0.0.1:6000", readme)
        self.assertNotIn("docker-compose.prod.yml", readme)
        self.assertNotIn("`docker-compose.yml`", readme)

    def test_restart_scripts_recreate_containers_to_reload_env(self):
        """restart 脚本必须重建容器，compose restart 不会加载新的 .env。"""
        for name in ("restart-dev.sh", "restart-master.sh"):
            source = (PROJECT_ROOT / name).read_text(encoding="utf-8")
            self.assertIn("up -d --force-recreate", source)
            self.assertNotIn("restart \"${SERVICES[@]}\"", source)


if __name__ == "__main__":
    unittest.main()
