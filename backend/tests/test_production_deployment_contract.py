"""Online image deployment contract tests."""

import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ONLINE_COMPOSE = PROJECT_ROOT / "docker-compose-prod.yaml"


class ProductionDeploymentContractTests(unittest.TestCase):
    """Validate the online Compose, runtime images, and release bundle boundary."""

    def test_online_compose_only_uses_prebuilt_backend_images(self):
        self.assertTrue(ONLINE_COMPOSE.is_file())
        source = ONLINE_COMPOSE.read_text(encoding="utf-8")

        self.assertNotIn("build:", source)
        self.assertNotRegex(source, r"(?m)^  (mysql|redis):\s*$")
        for service in ("migrate", "api", "worker-analysis", "worker-skill", "beat"):
            self.assertRegex(source, rf"(?m)^  {re.escape(service)}:\s*$")
        self.assertEqual(source.count("image: ${BACKEND_IMAGE}"), 5)

    def test_online_services_use_host_network(self):
        source = ONLINE_COMPOSE.read_text(encoding="utf-8")

        self.assertEqual(source.count('network_mode: "host"'), 5)
        self.assertIn('"${API_PORT}:5000"', source)
        # host 网络下 ports 映射不生效，gunicorn 必须直接监听 API_PORT。
        self.assertIn("0.0.0.0:${API_PORT:-5000}", source)

    def test_restart_scripts_remove_obsolete_worker_services(self):
        """重建时必须移除旧版 worker 容器，避免旧消费者继续占用错误队列。"""
        for name in ("restart-master.sh", "restart-dev.sh"):
            source = (PROJECT_ROOT / name).read_text(encoding="utf-8")
            self.assertIn("--remove-orphans", source)

    def test_all_production_services_read_users_from_image_configured_path(self):
        """所有启动 Flask 应用的服务都必须把用户文件挂到 AUTH_USERS_FILE 指定位置。"""
        source = ONLINE_COMPOSE.read_text(encoding="utf-8")

        self.assertEqual(source.count("./data/users.csv:/data/users.csv:ro"), 5)
        self.assertNotIn(":/users.csv", source)

    def test_frontend_image_supports_template_upstreams(self):
        template = (PROJECT_ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
        dockerfile = (PROJECT_ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("${FRONTEND_LISTEN_PORT}", template)
        self.assertIn("${API_UPSTREAM}", template)
        self.assertIn("/etc/nginx/templates/default.conf.template", dockerfile)

    def test_frontend_dockerfile_uses_valid_shell_healthcheck_syntax(self):
        dockerfile = (PROJECT_ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")

        self.assertNotIn("CMD-SHELL", dockerfile)
        self.assertIn('CMD wget -qO- "http://127.0.0.1:${FRONTEND_LISTEN_PORT}/health"', dockerfile)

    def test_backend_runtime_stage_copies_only_runtime_files(self):
        dockerfile = (PROJECT_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
        ignore = (PROJECT_ROOT / "backend" / ".dockerignore").read_text(encoding="utf-8")

        self.assertIn("AS build", dockerfile)
        self.assertIn("AS runtime", dockerfile)
        self.assertIn("COPY --from=build /app/app /app/app", dockerfile)
        self.assertIn("AUTH_USERS_FILE=/data/users.csv", dockerfile)
        self.assertIn("users.csv", ignore.splitlines())

    def test_backend_build_removes_generated_logs_and_python_caches(self):
        dockerfile = (PROJECT_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("rm -rf /app/app/logs/*", dockerfile)
        self.assertIn("-name '__pycache__'", dockerfile)

    def test_online_deploy_bundle_contains_runtime_files_only(self):
        required_files = (
            "docker-compose-prod.yaml",
            "restart-master.sh",
            "deploy/.env.production.example",
            "deploy/users.example.csv",
            "deploy/README.md",
            "scripts/build_release_images.ps1",
            "scripts/export_release_image.ps1",
            "scripts/package_production_deploy.ps1",
        )
        for relative_path in required_files:
            self.assertTrue((PROJECT_ROOT / relative_path).is_file(), relative_path)

        build_script = (PROJECT_ROOT / "scripts" / "build_release_images.ps1").read_text(encoding="utf-8")
        export_script = (PROJECT_ROOT / "scripts" / "export_release_image.ps1").read_text(encoding="utf-8")
        package_script = (PROJECT_ROOT / "scripts" / "package_production_deploy.ps1").read_text(encoding="utf-8")
        self.assertIn('ValidateSet("all", "backend", "frontend")', build_script)
        self.assertIn("docker push", build_script)
        self.assertIn("docker save", export_script)
        self.assertIn("docker-compose-prod.yaml", package_script)
        self.assertIn("restart-master.sh", package_script)
        self.assertNotIn("backend/app", package_script)

    def test_production_environment_example_is_not_git_ignored(self):
        gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertIn("!deploy/.env.production.example", gitignore.splitlines())


if __name__ == "__main__":
    unittest.main()
