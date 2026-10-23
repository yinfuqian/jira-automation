import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from flask import Flask

from extensions import db
from app.branches.models.model import Branch
from app.git import gitlab_sync
from app.git.gitlab_sync import (
    build_gitlab_project_query,
    filter_projects_missing_repositories,
    fetch_gitlab_projects,
    normalize_gitlab_project,
    sync_gitlab_projects,
    sync_gitlab_projects_from_config,
)
from app.modules.models import Module
from app.product.models import Product
from app.relasionship.models.model import ModuleBranch, ProductModule


class GitLabSyncTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        self.app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def test_normalize_gitlab_project_maps_group_project_and_repo_url(self):
        project = {
            "name": "dialog-service",
            "path_with_namespace": "bot-platform/backend/dialog-service",
            "http_url_to_repo": "https://code.in.wezhuiyi.com/bot-platform/backend/dialog-service.git",
            "default_branch": "master",
        }

        normalized = normalize_gitlab_project(project)

        self.assertEqual(normalized["product_name"], "bot-platform")
        self.assertEqual(normalized["module_name"], "dialog-service")
        self.assertEqual(
            normalized["repo_url"],
            "https://code.in.wezhuiyi.com/bot-platform/backend/dialog-service.git",
        )
        self.assertEqual(normalized["default_branch"], "master")

    def test_build_gitlab_project_query_defaults_to_all_visible_projects(self):
        query = build_gitlab_project_query(page=2)

        self.assertNotIn("membership", query)
        self.assertEqual(query["simple"], "true")
        self.assertEqual(query["page"], 2)

    def test_fetch_gitlab_projects_retries_transient_timeout(self):
        headers = {"X-Next-Page": ""}
        calls = {"count": 0}

        def flaky_get_json(api_url, params, request_headers, timeout):
            calls["count"] += 1
            if calls["count"] == 1:
                raise TimeoutError("timed out")
            return ([{
                "name": "dialog-service",
                "path_with_namespace": "bot/dialog-service",
                "http_url_to_repo": "https://code.in.wezhuiyi.com/bot/dialog-service.git",
            }], headers)

        with patch.object(gitlab_sync, "_get_json", side_effect=flaky_get_json):
            projects = fetch_gitlab_projects(
                "https://code.in.wezhuiyi.com/",
                private_token="token",
                timeout=1,
                max_retries=2,
                retry_delay=0,
            )

        self.assertEqual(len(projects), 1)
        self.assertEqual(calls["count"], 2)

    def test_fetch_gitlab_projects_fetches_known_pages_concurrently(self):
        page_barrier = threading.Barrier(2)
        state = {"active": 0, "max_active": 0}
        state_lock = threading.Lock()

        def concurrent_get_json(api_url, params, request_headers, timeout):
            page = params["page"]
            if page == 1:
                return ([{"name": "page-1", "path_with_namespace": "bot/page-1"}], {
                    "X-Total-Pages": "3",
                    "X-Next-Page": "2",
                })

            with state_lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            try:
                page_barrier.wait(timeout=2)
                time.sleep(0.02)
                return ([{"name": f"page-{page}", "path_with_namespace": f"bot/page-{page}"}], {
                    "X-Total-Pages": "3",
                    "X-Next-Page": str(page + 1) if page < 3 else "",
                })
            finally:
                with state_lock:
                    state["active"] -= 1

        with patch.object(gitlab_sync, "_get_json", side_effect=concurrent_get_json):
            projects = fetch_gitlab_projects(
                "https://code.in.wezhuiyi.com/",
                private_token="token",
                timeout=1,
                max_retries=0,
                retry_delay=0,
                page_concurrency=2,
            )

        self.assertEqual(state["max_active"], 2)
        self.assertEqual([project["name"] for project in projects], ["page-1", "page-2", "page-3"])

    def test_sync_gitlab_projects_upserts_products_modules_branches_and_relationships(self):
        projects = [
            {
                "name": "dialog-service",
                "path_with_namespace": "bot-platform/backend/dialog-service",
                "http_url_to_repo": "https://code.in.wezhuiyi.com/bot-platform/backend/dialog-service.git",
                "default_branch": "master",
            }
        ]

        result = sync_gitlab_projects(projects)
        second_result = sync_gitlab_projects(projects)

        product = Product.query.filter_by(name="bot-platform").one()
        module = Module.query.filter_by(name="dialog-service").one()
        branch = Branch.query.filter_by(
            address="https://code.in.wezhuiyi.com/bot-platform/backend/dialog-service.git"
        ).one()

        self.assertEqual(branch.tag_version, "master")
        self.assertEqual(ProductModule.query.filter_by(product_id=product.id, module_id=module.id).count(), 1)
        self.assertEqual(ModuleBranch.query.filter_by(module_id=module.id, branch_id=branch.id).count(), 1)
        self.assertEqual(result["created_products"], 1)
        self.assertEqual(result["created_modules"], 1)
        self.assertEqual(result["created_branches"], 1)
        self.assertEqual(second_result["created_products"], 0)
        self.assertEqual(second_result["created_modules"], 0)
        self.assertEqual(second_result["created_branches"], 0)

    def test_filter_projects_missing_repositories_skips_existing_branch_addresses(self):
        existing = Branch(
            address="https://code.in.wezhuiyi.com/bot/existing.git",
            tag_version="master",
        )
        db.session.add(existing)
        db.session.commit()
        projects = [
            {
                "name": "existing",
                "path_with_namespace": "bot/existing",
                "http_url_to_repo": "https://code.in.wezhuiyi.com/bot/existing.git",
            },
            {
                "name": "new",
                "path_with_namespace": "bot/new",
                "http_url_to_repo": "https://code.in.wezhuiyi.com/bot/new.git",
            },
        ]

        missing, skipped_count = filter_projects_missing_repositories(projects)

        self.assertEqual(skipped_count, 1)
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["name"], "new")

    def test_empty_products_table_forces_full_bootstrap_even_when_branch_exists(self):
        repo_url = "https://code.in.wezhuiyi.com/aiforce/aiforce2.git"
        db.session.add(Branch(address=repo_url, tag_version="master"))
        db.session.commit()
        projects = [{
            "name": "aiforce2",
            "path_with_namespace": "aiforce/aiforce2",
            "http_url_to_repo": repo_url,
            "default_branch": "master",
        }]
        config = {
            "GIT_BASE_URL": "https://code.in.wezhuiyi.com/",
            "GITLAB_SYNC_SKIP_EXISTING_REPOS": True,
        }

        with patch.object(gitlab_sync, "fetch_gitlab_projects", return_value=projects):
            result = sync_gitlab_projects_from_config(config)

        self.assertEqual(result["created_products"], 1)
        self.assertEqual(result["created_modules"], 1)
        self.assertEqual(result["existing_repositories_skipped"], 0)


if __name__ == "__main__":
    unittest.main()
