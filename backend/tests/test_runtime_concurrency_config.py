import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RuntimeConcurrencyConfigTests(unittest.TestCase):
    def test_online_compose_api_uses_configurable_gunicorn_settings(self):
        compose = (PROJECT_ROOT / "docker-compose-dev.yaml").read_text(encoding="utf-8")

        self.assertIn("- ${WEB_CONCURRENCY}", compose)
        self.assertIn("- gevent", compose)

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


if __name__ == "__main__":
    unittest.main()
