import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RuntimeConcurrencyConfigTests(unittest.TestCase):
    def test_online_compose_api_uses_configurable_gunicorn_settings(self):
        compose = (PROJECT_ROOT / "docker-compose-dev.yaml").read_text(encoding="utf-8")
        production_compose = (PROJECT_ROOT / "docker-compose-prod.yaml").read_text(encoding="utf-8")

        for content in (compose, production_compose):
            self.assertIn("- ${WEB_CONCURRENCY}", content)
            self.assertIn("- gthread", content)
            self.assertIn("--threads", content)
            self.assertIn("${WEB_THREADS}", content)
            self.assertIn('      - "300"', content)

    def test_backend_start_scripts_keep_upload_concurrency_tunable(self):
        start_sh = (PROJECT_ROOT / "backend" / "start.sh").read_text(encoding="utf-8")
        dockerfile = (PROJECT_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
        app_py = (PROJECT_ROOT / "backend" / "app.py").read_text(encoding="utf-8")

        for content in (start_sh, dockerfile):
            self.assertIn("WEB_CONCURRENCY", content)
            self.assertIn("GUNICORN_WORKER_CLASS", content)
            self.assertIn("WEB_THREADS", content)
            self.assertIn("GUNICORN_TIMEOUT", content)

        self.assertIn("threaded=True", app_py)

    def test_compose_splits_analysis_and_skill_workers_with_dedicated_queues(self):
        for filename in ("docker-compose-dev.yaml", "docker-compose-prod.yaml"):
            content = (PROJECT_ROOT / filename).read_text(encoding="utf-8")

            self.assertIn("analysis,maintenance", content)
            self.assertIn("--concurrency=2", content)
            self.assertIn("      - skill\n", content)
            self.assertNotIn("CELERY_ANALYSIS_CONCURRENCY", content)
            self.assertNotIn("CELERY_SKILL_CONCURRENCY", content)
            self.assertNotIn("${CELERY_WORKER_CONCURRENCY}", content)

    def test_environment_examples_only_keep_operational_settings(self):
        removed_keys = {
            "GUNICORN_WORKER_CLASS",
            "GUNICORN_TIMEOUT",
            "CELERY_ANALYSIS_CONCURRENCY",
            "CELERY_SKILL_CONCURRENCY",
            "CELERY_WORKER_CONCURRENCY",
            "PYTHON_BASE_IMAGE",
            "NODE_BASE_IMAGE",
            "APT_MIRROR",
            "PIP_INDEX_URL",
            "STORAGE_TYPE",
            "LOCAL_STORAGE_DIR",
            "SQLALCHEMY_POOL_RECYCLE",
            "LOCAL_OCR_ENABLED",
            "OCR_MAX_SIDE_LIMIT",
            "OCR_MIN_CONFIDENCE",
            "OCR_GPT_FALLBACK_CONFIDENCE",
            "SKILL_ARTIFACT_TICKET_SECRET",
            "ANALYSIS_API_BASE_URL",
            "ANALYSIS_API_TOKEN",
            "JIRA_WEBHOOK_SECRET",
            "CODEX_MODEL_PROVIDER",
            "CODEX_WIRE_API",
            "CODEX_SANDBOX",
            "CODEX_SKILL_TIMEOUT",
            "CODEX_NETWORK_RETRY_LIMIT",
            "DEVOPS_MCP_URL",
        }

        for filename in (".env.example", "deploy/.env.production.example"):
            content = (PROJECT_ROOT / filename).read_text(encoding="utf-8")
            assignments = [
                line.split("=", 1)[0].strip()
                for line in content.splitlines()
                if line.strip() and not line.lstrip().startswith("#") and "=" in line
            ]
            self.assertLessEqual(len(assignments), 36, filename)
            for key in removed_keys:
                self.assertNotIn(f"{key}=", content, f"{filename}: {key}")


if __name__ == "__main__":
    unittest.main()
