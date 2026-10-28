import logging
import io
import sys
import tempfile
import unittest
from pathlib import Path

from flask import Flask


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import setup_logging
from app.observability.request_logging import install_request_logging


class RequestLoggingTests(unittest.TestCase):
    def tearDown(self):
        root = logging.getLogger()
        for handler in list(root.handlers):
            if getattr(handler, "_jira_automation_handler", False):
                root.removeHandler(handler)
                handler.close()

    def test_setup_logging_routes_root_info_messages_to_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = Flask(__name__)
            app.config.update(
                LOG_DIR=temp_dir,
                LOGGING_FILE=str(Path(temp_dir) / "app.log"),
                LOGGING_LEVEL="INFO",
                LOGGING_FORMAT="%(levelname)s %(name)s %(message)s",
            )

            setup_logging(app)
            logging.getLogger("app.git.gitlab_sync").info("sync-progress-visible")
            for handler in logging.getLogger().handlers:
                handler.flush()

            content = Path(app.config["LOGGING_FILE"]).read_text(encoding="utf-8")
            self.assertIn("sync-progress-visible", content)

    def test_setup_logging_suppresses_celery_signature_debug_noise(self):
        """Celery 生成任务签名时会打印伪造的 return 1 源码，不应进入业务 DEBUG 日志。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            app = Flask(__name__)
            app.config.update(
                LOG_DIR=temp_dir,
                LOGGING_FILE=str(Path(temp_dir) / "app.log"),
                LOGGING_LEVEL="DEBUG",
                LOGGING_FORMAT="%(levelname)s %(name)s %(message)s",
            )

            setup_logging(app)

            self.assertEqual(
                logging.getLogger("celery.utils.functional").level,
                logging.INFO,
            )

    def test_request_logging_records_start_end_and_request_id(self):
        app = Flask(__name__)
        install_request_logging(app)

        @app.get("/probe")
        def probe():
            return {"ok": True}

        with self.assertLogs("jira_automation.request", level="INFO") as captured:
            response = app.test_client().get("/probe")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["X-Request-ID"].startswith("REQ-"))
        output = "\n".join(captured.output)
        self.assertIn("request_started", output)
        self.assertIn("request_completed", output)
        self.assertIn("path=/probe", output)
        self.assertIn("status=200", output)

    def test_setup_logging_reuses_existing_stream_handler(self):
        root = logging.getLogger()
        original_handlers = list(root.handlers)
        for handler in original_handlers:
            root.removeHandler(handler)
        existing = logging.StreamHandler(io.StringIO())
        root.addHandler(existing)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                app = Flask(__name__)
                app.config.update(
                    LOG_DIR=temp_dir,
                    LOGGING_FILE=str(Path(temp_dir) / "app.log"),
                    LOGGING_LEVEL="INFO",
                    LOGGING_FORMAT="%(levelname)s %(name)s %(message)s",
                )
                setup_logging(app)

                duplicated = [
                    handler for handler in root.handlers
                    if getattr(handler, "_jira_automation_stream", False)
                ]
                self.assertEqual(duplicated, [])
        finally:
            root.removeHandler(existing)
            existing.close()
            for handler in original_handlers:
                root.addHandler(handler)

    def test_setup_logging_adds_info_stream_when_existing_handler_only_has_warning(self):
        root = logging.getLogger()
        original_handlers = list(root.handlers)
        for handler in original_handlers:
            root.removeHandler(handler)
        existing = logging.StreamHandler(io.StringIO())
        existing.setLevel(logging.WARNING)
        root.addHandler(existing)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                app = Flask(__name__)
                app.config.update(
                    LOG_DIR=temp_dir,
                    LOGGING_FILE=str(Path(temp_dir) / "app.log"),
                    LOGGING_LEVEL="INFO",
                    LOGGING_FORMAT="%(levelname)s %(name)s %(message)s",
                )
                setup_logging(app)

                info_streams = [
                    handler for handler in root.handlers
                    if getattr(handler, "_jira_automation_stream", False)
                ]
                self.assertEqual(len(info_streams), 1)
        finally:
            root.removeHandler(existing)
            existing.close()
            for handler in original_handlers:
                root.addHandler(handler)


if __name__ == "__main__":
    unittest.main()
