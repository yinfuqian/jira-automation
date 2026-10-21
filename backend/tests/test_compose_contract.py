import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEV_COMPOSE_PATH = PROJECT_ROOT / "docker-compose-dev.yaml"
PROD_COMPOSE_PATH = PROJECT_ROOT / "docker-compose-prod.yaml"


class ComposeContractTests(unittest.TestCase):
    def test_online_compose_declares_expected_service_graph(self):
        source = DEV_COMPOSE_PATH.read_text(encoding="utf-8")

        for service in ("api", "worker", "migrate"):
            self.assertRegex(source, rf"(?m)^  {re.escape(service)}:\s*$")
        self.assertNotRegex(source, r"(?m)^  (mysql|redis|frontend):\s*$")

    def test_online_compose_uses_prebuilt_backend_image_and_host_network(self):
        source = DEV_COMPOSE_PATH.read_text(encoding="utf-8")

        self.assertNotIn("build:", source)
        self.assertEqual(source.count("image: ${BACKEND_IMAGE}"), 3)
        self.assertEqual(source.count('network_mode: "host"'), 3)

    def test_online_compose_mounts_runtime_data_and_skills(self):
        source = DEV_COMPOSE_PATH.read_text(encoding="utf-8")

        self.assertEqual(source.count("env_file:\n      - ${ENV_FILE:-.env}"), 3)
        for mount in (
            "./data/users.csv:/users.csv",
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

    def test_readme_uses_online_compose_and_environment_specific_restart_scripts(self):
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertEqual(DEV_COMPOSE_PATH.read_bytes(), PROD_COMPOSE_PATH.read_bytes())
        self.assertIn("docker-compose-dev.yaml", readme)
        self.assertIn("docker-compose-prod.yaml", readme)
        self.assertIn("restart-dev.sh", readme)
        self.assertIn("restart-master.sh", readme)
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
