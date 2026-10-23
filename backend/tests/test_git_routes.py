import unittest
import sys
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]

from app import create_app
from app.git import routes as git_routes
from app.git.routes import (
    build_git_ref_key,
    build_authenticated_git_url,
    load_cached_refs,
    list_remote_branches,
    list_remote_refs,
    parse_ls_remote_heads,
    parse_ls_remote_refs,
    save_refs_to_cache,
)
from app.git.models import GitRef
from extensions import db


def create_test_app():
    return create_app(config_overrides={"TESTING": True, "AUTH_TEST_BYPASS": True})


class GitRoutesTests(unittest.TestCase):
    def test_build_authenticated_git_url_injects_configured_credentials(self):
        url = build_authenticated_git_url(
            "https://code.in.wezhuiyi.com/group/repo.git",
            "git-user",
            "git-pass",
        )

        self.assertEqual(
            url,
            "https://git-user:git-pass@code.in.wezhuiyi.com/group/repo.git",
        )

    def test_parse_ls_remote_heads_returns_branch_names(self):
        output = (
            "abc123\trefs/heads/master\n"
            "def456\trefs/heads/feature/log-analyzer\n"
            "ghi789\trefs/tags/v1.0\n"
        )

        self.assertEqual(
            parse_ls_remote_heads(output),
            ["master", "feature/log-analyzer"],
        )

    def test_parse_ls_remote_refs_returns_branch_and_tag_names(self):
        output = (
            "abc123\trefs/heads/master\n"
            "def456\trefs/heads/release/demo\n"
            "ghi789\trefs/tags/v1.0\n"
            "jkl012\trefs/tags/v1.0^{}\n"
            "mno345\trefs/tags/v5.136.7-ZYKJ20230046-rc1\n"
        )

        refs = parse_ls_remote_refs(output)

        self.assertEqual(refs["branches"], ["master", "release/demo"])
        self.assertEqual(refs["tags"], ["v1.0", "v5.136.7-ZYKJ20230046-rc1"])
        self.assertEqual(
            refs["versions"],
            ["master", "release/demo", "v1.0", "v5.136.7-ZYKJ20230046-rc1"],
        )

    def test_build_git_ref_key_is_short_and_stable_for_long_urls(self):
        repo_url = "https://code.in.wezhuiyi.com/" + "/".join([f"group{i}" for i in range(40)]) + "/repo.git"

        first = build_git_ref_key(repo_url, "branch", "release_zyzx_zgyz/1.3.2")
        second = build_git_ref_key(repo_url, "branch", "release_zyzx_zgyz/1.3.2")
        tag_key = build_git_ref_key(repo_url, "tag", "release_zyzx_zgyz/1.3.2")

        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, tag_key)

    def test_list_remote_branches_runs_git_ls_remote_with_credentials(self):
        with patch.object(git_routes.subprocess, "run") as run:
            run.return_value = SimpleNamespace(
                stdout="abc123\trefs/heads/master\n",
                stderr="",
            )

            branches = list_remote_branches(
                "https://code.in.wezhuiyi.com/group/repo.git",
                username="git-user",
                password="git-pass",
            )

            self.assertEqual(branches, ["master"])
            args = run.call_args.args[0]
            self.assertEqual(args[0:3], ["git", "ls-remote", "--heads"])
            self.assertIn("git-user:git-pass@", args[-1])

    def test_list_remote_refs_runs_git_ls_remote_for_heads_and_tags(self):
        with patch.object(git_routes.subprocess, "run") as run:
            run.return_value = SimpleNamespace(
                stdout=(
                    "abc123\trefs/heads/master\n"
                    "def456\trefs/tags/v1.1.0\n"
                ),
                stderr="",
            )

            refs = list_remote_refs(
                "https://code.in.wezhuiyi.com/group/repo.git",
                username="git-user",
                password="git-pass",
            )

            self.assertEqual(refs["versions"], ["master", "v1.1.0"])
            args = run.call_args.args[0]
            self.assertEqual(args[0:4], ["git", "ls-remote", "--heads", "--tags"])
            self.assertIn("git-user:git-pass@", args[4])

    def test_git_branches_endpoint_returns_remote_branches_and_tags_without_credentials(self):
        with patch.object(git_routes, "load_cached_refs", return_value=None):
            with patch.object(git_routes, "save_refs_to_cache") as save_cache:
                with patch.object(git_routes.subprocess, "run") as run:
                    run.return_value = SimpleNamespace(
                        stdout=(
                            "abc123\trefs/heads/master\n"
                            "def456\trefs/tags/v1.1.0\n"
                        ),
                        stderr="",
                    )
                    if "flask" in sys.modules and not hasattr(sys.modules["flask"], "g"):
                        del sys.modules["flask"]
                    if "celery.result" in sys.modules and not hasattr(sys.modules["celery.result"], "GroupResult"):
                        del sys.modules["celery.result"]
                    app = create_test_app()

                    response = app.test_client().get(
                        "/git/branches",
                        query_string={"repo_url": "https://code.in.wezhuiyi.com/group/repo.git"},
                    )

                    self.assertEqual(response.status_code, 200)
                    payload = response.get_json()
                    self.assertEqual(payload["branches"], ["master"])
                    self.assertEqual(payload["tags"], ["v1.1.0"])
                    self.assertEqual(payload["versions"], ["master", "v1.1.0"])
                    self.assertEqual(payload["source"], "remote")
                    self.assertEqual(payload["repo_url"], "https://code.in.wezhuiyi.com/group/repo.git")
                    self.assertNotIn("git-pass", response.get_data(as_text=True))
                    save_cache.assert_called_once()

    def test_git_branches_endpoint_returns_cached_refs_without_remote_call(self):
        cached_refs = {
            "branches": ["master"],
            "tags": ["v1.1.0"],
            "versions": ["master", "v1.1.0"],
        }

        with patch.object(git_routes, "load_cached_refs", return_value=cached_refs):
            with patch.object(git_routes.subprocess, "run") as run:
                if "flask" in sys.modules and not hasattr(sys.modules["flask"], "g"):
                    del sys.modules["flask"]
                if "celery.result" in sys.modules and not hasattr(sys.modules["celery.result"], "GroupResult"):
                    del sys.modules["celery.result"]
                app = create_test_app()

                response = app.test_client().get(
                    "/git/branches",
                    query_string={"repo_url": "https://code.in.wezhuiyi.com/group/repo.git"},
                )

                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload["versions"], ["master", "v1.1.0"])
                self.assertEqual(payload["source"], "database")
                run.assert_not_called()

    def test_git_branches_cached_only_returns_empty_without_remote_call(self):
        with patch.object(git_routes, "load_cached_refs", return_value=None):
            with patch.object(git_routes.subprocess, "run") as run:
                if "flask" in sys.modules and not hasattr(sys.modules["flask"], "g"):
                    del sys.modules["flask"]
                if "celery.result" in sys.modules and not hasattr(sys.modules["celery.result"], "GroupResult"):
                    del sys.modules["celery.result"]
                app = create_test_app()

                response = app.test_client().get(
                    "/git/branches",
                    query_string={
                        "repo_url": "https://code.in.wezhuiyi.com/group/repo.git",
                        "cached_only": "1",
                    },
                )

                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload["source"], "database")
                self.assertFalse(payload["cached"])
                self.assertEqual(payload["versions"], [])
                run.assert_not_called()

    def test_git_branches_endpoint_refresh_bypasses_cached_refs(self):
        cached_refs = {
            "branches": ["old"],
            "tags": [],
            "versions": ["old"],
        }

        with patch.object(git_routes, "load_cached_refs", return_value=cached_refs):
            with patch.object(git_routes, "save_refs_to_cache"):
                with patch.object(git_routes.subprocess, "run") as run:
                    run.return_value = SimpleNamespace(
                        stdout="abc123\trefs/heads/master\n",
                        stderr="",
                    )
                    if "flask" in sys.modules and not hasattr(sys.modules["flask"], "g"):
                        del sys.modules["flask"]
                    if "celery.result" in sys.modules and not hasattr(sys.modules["celery.result"], "GroupResult"):
                        del sys.modules["celery.result"]
                    app = create_test_app()

                    response = app.test_client().get(
                        "/git/branches",
                        query_string={
                            "repo_url": "https://code.in.wezhuiyi.com/group/repo.git",
                            "refresh": "1",
                        },
                    )

                    self.assertEqual(response.status_code, 200)
                    payload = response.get_json()
                    self.assertEqual(payload["versions"], ["master"])
                    self.assertEqual(payload["source"], "remote")
                    run.assert_called_once()

    def test_save_and_load_refs_cache_round_trips_branch_and_tag_types(self):
        from flask import Flask

        app = Flask(__name__)
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        db.init_app(app)

        with app.app_context():
            db.create_all()
            save_refs_to_cache(
                "https://code.in.wezhuiyi.com/group/repo.git",
                {
                    "branches": ["master", "release/demo"],
                    "tags": ["v1.1.0"],
                    "versions": ["master", "release/demo", "v1.1.0"],
                },
            )
            save_refs_to_cache(
                "https://code.in.wezhuiyi.com/group/repo.git",
                {
                    "branches": ["master"],
                    "tags": ["v1.1.0", "v1.2.0"],
                    "versions": ["master", "v1.1.0", "v1.2.0"],
                },
            )

            refs = load_cached_refs("https://code.in.wezhuiyi.com/group/repo.git")

            self.assertEqual(refs["branches"], ["master"])
            self.assertEqual(refs["tags"], ["v1.1.0", "v1.2.0"])
            self.assertEqual(
                refs["versions"],
                ["master", "v1.1.0", "v1.2.0"],
            )
            self.assertEqual(GitRef.query.count(), 3)
            self.assertTrue(all(len(item.ref_key) == 64 for item in GitRef.query.all()))

            db.session.remove()
            db.drop_all()

    def test_git_branches_endpoint_returns_cached_refs_when_refresh_remote_fails(self):
        cached_refs = {
            "branches": ["master"],
            "tags": ["v1.1.0"],
            "versions": ["master", "v1.1.0"],
        }

        with patch.object(git_routes, "load_cached_refs", return_value=cached_refs):
            with patch.object(git_routes.subprocess, "run") as run:
                run.side_effect = subprocess.CalledProcessError(
                    128,
                    ["git", "ls-remote"],
                    stderr="fatal: repository not found",
                )
                if "flask" in sys.modules and not hasattr(sys.modules["flask"], "g"):
                    del sys.modules["flask"]
                if "celery.result" in sys.modules and not hasattr(sys.modules["celery.result"], "GroupResult"):
                    del sys.modules["celery.result"]
                app = create_test_app()

                response = app.test_client().get(
                    "/git/branches",
                    query_string={
                        "repo_url": "https://code.in.wezhuiyi.com/group/repo.git",
                        "refresh": "1",
                    },
                )

                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload["source"], "database")
                self.assertTrue(payload["stale"])
                self.assertEqual(payload["versions"], ["master", "v1.1.0"])

    def test_git_branches_endpoint_returns_502_without_cache_when_remote_fails(self):
        with patch.object(git_routes, "load_cached_refs", return_value=None):
            with patch.object(git_routes.subprocess, "run") as run:
                run.side_effect = subprocess.CalledProcessError(
                    128,
                    ["git", "ls-remote"],
                    stderr="fatal: repository not found",
                )
                if "flask" in sys.modules and not hasattr(sys.modules["flask"], "g"):
                    del sys.modules["flask"]
                if "celery.result" in sys.modules and not hasattr(sys.modules["celery.result"], "GroupResult"):
                    del sys.modules["celery.result"]
                app = create_test_app()

                response = app.test_client().get(
                    "/git/branches",
                    query_string={"repo_url": "https://code.in.wezhuiyi.com/group/repo.git"},
                )

                self.assertEqual(response.status_code, 502)

    def test_git_sync_projects_endpoint_returns_sync_counts(self):
        if "flask" in sys.modules and not hasattr(sys.modules["flask"], "g"):
            del sys.modules["flask"]
        if "celery.result" in sys.modules and not hasattr(sys.modules["celery.result"], "GroupResult"):
            del sys.modules["celery.result"]

        with patch.object(git_routes, "run_gitlab_sync_once") as sync:
            sync.return_value = {
                "status": "succeeded",
                "fetched_projects": 1,
                "synced_projects": 1,
                "created_products": 1,
                "created_modules": 1,
                "created_branches": 1,
            }
            app = create_test_app()

            response = app.test_client().post("/git/sync-projects")

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["synced_projects"], 1)
            self.assertEqual(payload["created_products"], 1)
            self.assertNotIn("git-pass", response.get_data(as_text=True))

    def test_git_sync_projects_endpoint_returns_409_when_sync_lock_is_busy(self):
        if "flask" in sys.modules and not hasattr(sys.modules["flask"], "g"):
            del sys.modules["flask"]
        if "celery.result" in sys.modules and not hasattr(sys.modules["celery.result"], "GroupResult"):
            del sys.modules["celery.result"]

        with patch.object(git_routes, "run_gitlab_sync_once") as sync:
            sync.return_value = {"status": "skipped", "reason": "sync_in_progress"}
            app = create_test_app()

            response = app.test_client().post("/git/sync-projects")

            self.assertEqual(response.status_code, 409)
            payload = response.get_json()
            self.assertEqual(payload["code"], "GIT_SYNC_IN_PROGRESS")
            sync.assert_called_once()

    def test_git_sync_projects_endpoint_reports_missing_base_url(self):
        if "flask" in sys.modules and not hasattr(sys.modules["flask"], "g"):
            del sys.modules["flask"]
        if "celery.result" in sys.modules and not hasattr(sys.modules["celery.result"], "GroupResult"):
            del sys.modules["celery.result"]

        with patch.object(git_routes, "run_gitlab_sync_once") as sync:
            sync.return_value = {"status": "skipped", "reason": "git_base_url_missing"}
            app = create_test_app()

            response = app.test_client().post("/git/sync-projects")

            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.get_json()["code"], "GIT_BASE_URL_MISSING")

    def test_git_sync_projects_endpoint_releases_sync_lock_after_success(self):
        if "flask" in sys.modules and not hasattr(sys.modules["flask"], "g"):
            del sys.modules["flask"]
        if "celery.result" in sys.modules and not hasattr(sys.modules["celery.result"], "GroupResult"):
            del sys.modules["celery.result"]

        with patch.object(git_routes, "run_gitlab_sync_once") as sync:
            sync.return_value = {
                "status": "succeeded",
                "fetched_projects": 1,
                "synced_projects": 1,
            }
            app = create_test_app()

            response = app.test_client().post("/git/sync-projects")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["synced_projects"], 1)
            sync.assert_called_once()


if __name__ == "__main__":
    unittest.main()
