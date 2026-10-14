import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class RuntimeChecksTests(unittest.TestCase):
    def test_build_checks_cover_all_core_runtime_components(self):
        from app.runtime_checks import run_build_checks

        checks = run_build_checks(
            python_checker=lambda: None,
            dependency_checker=lambda: None,
            application_checker=lambda: None,
            celery_checker=lambda: None,
            git_checker=lambda: None,
            java_checker=lambda: None,
            ocr_checker=lambda: None,
        )

        self.assertEqual(
            set(checks),
            {"python", "dependencies", "application", "celery", "git", "java", "ocr"},
        )
        self.assertTrue(all(item["ok"] for item in checks.values()))

    def test_build_checks_surface_java_toolchain_failure(self):
        """容器内缺少 JDK/Maven 时必须让构建检查失败，而不是等技能编译时才报错。"""
        from app.runtime_checks import run_build_checks

        def fail_java():
            raise RuntimeError("JDK 8 版本不符合预期：javac 11.0.22")

        checks = run_build_checks(
            python_checker=lambda: None,
            dependency_checker=lambda: None,
            application_checker=lambda: None,
            celery_checker=lambda: None,
            git_checker=lambda: None,
            java_checker=fail_java,
            ocr_checker=lambda: None,
        )

        self.assertFalse(checks["java"]["ok"])
        self.assertIn("JDK 8", checks["java"]["message"])

    def test_readiness_reports_each_failed_external_dependency(self):
        from app.runtime_checks import run_readiness_checks

        def fail_database():
            raise RuntimeError("database unavailable")

        result = run_readiness_checks(
            database_checker=fail_database,
            redis_checker=lambda: None,
            users_checker=lambda: None,
            storage_checker=lambda: None,
        )

        self.assertFalse(result["ready"])
        self.assertFalse(result["checks"]["database"]["ok"])
        self.assertNotIn("password", result["checks"]["database"]["message"].lower())

    def test_failed_check_messages_do_not_expose_connection_credentials(self):
        from app.runtime_checks import run_readiness_checks

        def fail_redis():
            raise RuntimeError("redis://default:secret-password@redis:6379/0 failed")

        result = run_readiness_checks(
            database_checker=lambda: None,
            redis_checker=fail_redis,
            users_checker=lambda: None,
            storage_checker=lambda: None,
        )

        message = result["checks"]["redis"]["message"]
        self.assertNotIn("secret-password", message)
        self.assertIn("***", message)


if __name__ == "__main__":
    unittest.main()
