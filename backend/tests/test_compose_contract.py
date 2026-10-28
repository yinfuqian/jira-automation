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
        self.assertEqual(source.count("image: ${BACKEND_IMAGE}"), 5)
        self.assertEqual(source.count('network_mode: "host"'), 5)
        self.assertIn("0.0.0.0:6000", source)
        self.assertIn('"${API_PORT}:6000"', source)

    def test_split_workers_have_unique_celery_node_names(self):
        """host 网络下两个 Worker 共用主机名，必须显式设置唯一节点名供巡检识别。"""
        expected = {
            DEV_COMPOSE_PATH: ("worker-analysis-dev@%h", "worker-skill-dev@%h"),
            PROD_COMPOSE_PATH: ("worker-analysis@%h", "worker-skill@%h"),
        }
        for path, hostnames in expected.items():
            source = path.read_text(encoding="utf-8")
            for hostname in hostnames:
                self.assertIn(f"--hostname={hostname}", source)

    def test_online_compose_mounts_runtime_data_and_skills(self):
        source = DEV_COMPOSE_PATH.read_text(encoding="utf-8")

        self.assertEqual(source.count("env_file:\n      - .env"), 5)
        self.assertEqual(source.count("./data/users-dev.csv:/data/users.csv:ro"), 5)
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
