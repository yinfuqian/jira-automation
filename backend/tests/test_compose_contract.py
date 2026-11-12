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
            "api",
            "worker-analysis",
            "worker-skill",
            "worker-chatbot",
            "chatbot-gateway",
            "worker-chatbot-knowledge",
            "chatbot-gateway-knowledge",
            "beat",
            "migrate",
        ):
            self.assertRegex(source, rf"(?m)^  {re.escape(service)}:\s*$")
        self.assertNotRegex(source, r"(?m)^  (mysql|redis|frontend):\s*$")
        self.assertIn("      - api", source)
        self.assertIn("      - worker-analysis", source)
        self.assertIn("      - worker-skill", source)

    def test_online_compose_uses_prebuilt_backend_image_and_host_network(self):
        source = DEV_COMPOSE_PATH.read_text(encoding="utf-8")

        self.assertNotIn("build:", source)
        self.assertEqual(source.count("image: ${BACKEND_IMAGE}"), 7)
        self.assertEqual(
            source.count("image: ${KNOWLEDGE_BACKEND_IMAGE:-${BACKEND_IMAGE}}"),
            2,
        )
        self.assertEqual(source.count('network_mode: "host"'), 9)
        self.assertIn("0.0.0.0:${API_PORT:-5000}", source)
        self.assertIn('"${API_PORT}:5000"', source)

    def test_split_workers_have_unique_celery_node_names(self):
        """host 网络下多个 Worker 共用主机名，必须显式设置唯一节点名供巡检识别。"""
        expected = {
            DEV_COMPOSE_PATH: (
                "worker-analysis@%h",
                "worker-skill@%h",
                "worker-chatbot@%h",
                "worker-chatbot-knowledge@%h",
            ),
            PROD_COMPOSE_PATH: (
                "worker-analysis@%h",
                "worker-skill@%h",
            ),
        }
        for path, hostnames in expected.items():
            source = path.read_text(encoding="utf-8")
            for hostname in hostnames:
                self.assertIn(f"--hostname={hostname}", source)

    def test_online_compose_mounts_runtime_data_and_skills(self):
        source = DEV_COMPOSE_PATH.read_text(encoding="utf-8")

        self.assertEqual(source.count("env_file:\n      - ${ENV_FILE:-.env}"), 9)
        self.assertEqual(source.count("./data/users.csv:/data/users.csv:ro"), 9)
        self.assertNotIn(":/users.csv", source)
        for mount in (
            "./data/upload:/data/upload",
            "./data/logs:/app/app/logs",
            "./data/repos:/tmp/jira-automation-repos",
            "./skills:/data/skills:ro",
            "./data/codex:/data/codex",
            "./data/skill-workspace:/data/skill-workspace",
        ):
            self.assertIn(mount, source)

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

    def test_chatbot_config_is_baked_into_backend_image(self):
        """项目映射随镜像构建，API、机器人 Worker 与网关都不再依赖目录挂载。"""
        config_path = PROJECT_ROOT / "backend" / "config" / "chatbot.json"
        self.assertTrue(config_path.is_file())
        dockerfile = (PROJECT_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(
            "COPY --from=build /app/config/chatbot.json /app/config/chatbot.json",
            dockerfile,
        )
        self.assertIn(
            "COPY --from=build /app/config/knowledge_bot.json /app/config/knowledge_bot.json",
            dockerfile,
        )
        self.assertIn(
            "COPY --from=build /app/config/knowledge_scenarios.json /app/config/knowledge_scenarios.json",
            dockerfile,
        )
        self.assertIn(
            "COPY --from=build /app/config/capacity_profiles.json /app/config/capacity_profiles.json",
            dockerfile,
        )
        for path in (DEV_COMPOSE_PATH, PROD_COMPOSE_PATH):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("/data/config", source)

    def test_start_script_selects_services_by_deploy_env(self):
        """启动脚本按 DEPLOY_ENV 选择生产或开发的服务名，并包含机器人服务。"""
        source = (PROJECT_ROOT / "start.sh").read_text(encoding="utf-8")
        self.assertIn("RUN_MIGRATIONS", source)
        self.assertIn("DEPLOY_ENV", source)
        self.assertIn("docker-compose-dev.yaml", source)
        self.assertIn("docker-compose-prod.yaml", source)
        self.assertIn("worker-chatbot", source)
        self.assertIn("chatbot-gateway", source)
        self.assertIn("worker-chatbot-knowledge", source)
        self.assertIn("chatbot-gateway-knowledge", source)
        # ps 不接受 up 专用参数，避免脚本在查询状态时直接失败。
        ps_line = next(line for line in source.splitlines() if " compose " in line and " ps " in line)
        self.assertNotIn("--force-recreate", ps_line)
        self.assertNotIn("--remove-orphans", ps_line)

    def test_production_restart_script_does_not_select_chatbot_services(self):
        """生产启动/重启不得自动拉起机器人服务。"""
        source = (PROJECT_ROOT / "restart-master.sh").read_text(encoding="utf-8")
        self.assertNotIn("worker-chatbot-dev", source)
        self.assertNotIn("chatbot-gateway-dev", source)

    def test_stop_script_uses_unified_service_names(self):
        source = (PROJECT_ROOT / "stop.sh").read_text(encoding="utf-8")
        self.assertNotIn('SUFFIX="-dev"', source)

    def test_chatbot_services_are_not_split_into_extra_compose_file(self):
        """开发环境在一个 Compose 文件中定义全部服务，生产环境不定义机器人服务。"""
        self.assertFalse((PROJECT_ROOT / "docker-compose-knowledge.yaml").exists())
        dev_source = DEV_COMPOSE_PATH.read_text(encoding="utf-8")
        prod_source = PROD_COMPOSE_PATH.read_text(encoding="utf-8")
        self.assertIn("worker-chatbot-knowledge:", dev_source)
        self.assertIn("chatbot-gateway-knowledge:", dev_source)
        self.assertNotIn("worker-chatbot-knowledge:", prod_source)
        self.assertNotIn("chatbot-gateway-knowledge:", prod_source)

    def test_readme_uses_online_compose_and_environment_specific_restart_scripts(self):
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

        dev_source = DEV_COMPOSE_PATH.read_text(encoding="utf-8")
        prod_source = PROD_COMPOSE_PATH.read_text(encoding="utf-8")
        self.assertIn("api:", dev_source)
        self.assertIn("worker-analysis:", dev_source)
        self.assertIn("worker-skill:", dev_source)
        self.assertIn("beat:", dev_source)
        self.assertIn("api:", prod_source)
        self.assertIn("beat:", prod_source)
        self.assertIn("docker-compose-dev.yaml", readme)
        self.assertIn("docker-compose-prod.yaml", readme)
        self.assertIn("restart-dev.sh", readme)
        self.assertIn("restart-master.sh", readme)
        self.assertIn("restart-dev.sh", readme)
        self.assertNotIn("docker-compose.prod.yml", readme)
        self.assertNotIn("`docker-compose.yml`", readme)

    def test_restart_scripts_recreate_containers_to_reload_env(self):
        """restart 脚本必须重建容器，compose restart 不会加载新的 .env。"""
        source = (PROJECT_ROOT / "restart.sh").read_text(encoding="utf-8")
        self.assertIn("up -d --force-recreate", source)
        self.assertNotIn("restart \"${SERVICES[@]}\"", source)
        for name in ("restart-dev.sh", "restart-master.sh"):
            wrapper = (PROJECT_ROOT / name).read_text(encoding="utf-8")
            self.assertIn("exec bash", wrapper)


if __name__ == "__main__":
    unittest.main()
