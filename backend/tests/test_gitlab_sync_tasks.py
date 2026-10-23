import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.git import tasks as git_tasks


class GitLabSyncTaskTests(unittest.TestCase):
    @patch.object(git_tasks, "release_sync_lock")
    @patch.object(git_tasks, "sync_gitlab_projects_from_config")
    @patch.object(git_tasks, "acquire_sync_lock")
    def test_run_gitlab_sync_once_uses_shared_lock(self, acquire, sync, release):
        lock = object()
        acquire.return_value = lock
        sync.return_value = {"fetched_projects": 3, "synced_projects": 2}
        config = {"GIT_BASE_URL": "https://git.example.com/"}

        with self.assertLogs(level="INFO") as captured:
            result = git_tasks.run_gitlab_sync_once(config)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["synced_projects"], 2)
        self.assertIn("GitLab 项目同步开始", "\n".join(captured.output))
        self.assertIn("GitLab 项目同步完成", "\n".join(captured.output))
        acquire.assert_called_once_with()
        sync.assert_called_once_with(config)
        release.assert_called_once_with(lock)

    @patch.object(git_tasks, "release_sync_lock")
    @patch.object(git_tasks, "sync_gitlab_projects_from_config")
    @patch.object(git_tasks, "acquire_sync_lock", return_value=None)
    def test_run_gitlab_sync_once_skips_when_another_sync_holds_lock(self, acquire, sync, release):
        result = git_tasks.run_gitlab_sync_once({"GIT_BASE_URL": "https://git.example.com/"})

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "sync_in_progress")
        sync.assert_not_called()
        release.assert_not_called()

    @patch.object(git_tasks, "release_sync_lock")
    @patch.object(git_tasks, "sync_gitlab_projects_from_config", side_effect=RuntimeError("boom"))
    @patch.object(git_tasks, "acquire_sync_lock")
    def test_run_gitlab_sync_once_releases_lock_when_sync_fails(self, acquire, sync, release):
        lock = object()
        acquire.return_value = lock

        with self.assertRaisesRegex(RuntimeError, "boom"):
            git_tasks.run_gitlab_sync_once({"GIT_BASE_URL": "https://git.example.com/"})

        release.assert_called_once_with(lock)

    @patch.object(git_tasks, "acquire_sync_lock")
    def test_run_gitlab_sync_once_skips_without_base_url(self, acquire):
        result = git_tasks.run_gitlab_sync_once({})

        self.assertEqual(result, {"status": "skipped", "reason": "git_base_url_missing"})
        acquire.assert_not_called()

    @patch.object(git_tasks, "run_gitlab_sync_once")
    def test_celery_task_returns_sync_result(self, run_sync):
        run_sync.return_value = {"status": "succeeded", "synced_projects": 1}
        app = Flask(__name__)
        app.config["GIT_BASE_URL"] = "https://git.example.com/"

        with app.app_context():
            result = git_tasks.sync_gitlab_projects_task.run()

        self.assertEqual(result["status"], "succeeded")
        run_sync.assert_called_once()

    @patch.object(git_tasks.sync_gitlab_projects_task, "delay")
    def test_startup_trigger_enqueues_sync(self, delay):
        app = Mock()
        app.config = {"GITLAB_SYNC_ENABLED": True, "GITLAB_SYNC_ON_STARTUP": True}
        celery_app = Mock()
        celery_app._gitlab_startup_sync_installed = False
        signal = Mock()

        with patch.object(git_tasks, "worker_ready", signal):
            installed = git_tasks.install_worker_startup_sync(app, celery_app)

        self.assertTrue(installed)
        handler = signal.connect.call_args.args[0]
        handler()
        delay.assert_called_once_with()

    @patch.object(git_tasks.sync_gitlab_projects_task, "delay")
    def test_startup_trigger_respects_automatic_sync_switch(self, delay):
        app = Mock()
        app.config = {"GITLAB_SYNC_ENABLED": False, "GITLAB_SYNC_ON_STARTUP": True}
        celery_app = Mock()
        celery_app._gitlab_startup_sync_installed = False
        signal = Mock()

        with patch.object(git_tasks, "worker_ready", signal):
            git_tasks.install_worker_startup_sync(app, celery_app)

        signal.connect.call_args.args[0]()
        delay.assert_not_called()


if __name__ == "__main__":
    unittest.main()
