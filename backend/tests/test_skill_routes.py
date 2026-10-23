"""技能接口测试：覆盖参数校验、任务提交、查询取消与外部令牌认证。"""
import hashlib
import hmac
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

try:
    from tests.skill_env_fixture import with_locked_skill_env
except ModuleNotFoundError:  # 兼容以 tests 目录为根直接运行
    from skill_env_fixture import with_locked_skill_env

PROJECT_ROOT = BACKEND_DIR.parent
SKILLS_DIR = PROJECT_ROOT / "skills"


def create_test_app(**overrides):
    """构造使用内存数据库并跳过登录校验的测试应用。"""
    from app import create_app

    config = {
        "TESTING": True,
        "AUTH_TEST_BYPASS": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SKILLS_DIR": str(SKILLS_DIR),
        "SKILL_WORKSPACE_DIR": tempfile.gettempdir(),
        "SKILL_API_TOKEN": "skill-secret",
        # 测试用例使用 jira.example.com 等外部域名，这里显式关闭白名单；
        # 白名单校验由 test_submit_rejects_non_http_url_or_host_outside_allowlist 单独覆盖。
        "SKILL_URL_ALLOWED_HOSTS": "",
    }
    # 技能运行参数取自测试夹具：缺配置时提交接口会直接返回 503，用例不应依赖本机 .env。
    config = with_locked_skill_env(config)
    config.update(overrides)
    return create_app(config_overrides=config)


class SkillRouteTests(unittest.TestCase):
    def setUp(self):
        """为每个用例准备独立的应用与内存表结构。"""
        from extensions import db

        self.app = create_test_app()
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.addCleanup(self._cleanup_context)
        self.client = self.app.test_client()

    def _cleanup_context(self):
        """退出应用上下文并销毁测试库连接。"""
        from extensions import db

        db.session.remove()
        self.context.pop()

    def test_list_skills_includes_builtin_jira_skill(self):
        """技能列表返回内置的 Jira 需求准入检查技能。"""
        response = self.client.get("/skill/list")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("jira-gate-1", [item["skill_id"] for item in payload["skills"]])

    def test_submit_requires_skill_id_and_jira_url(self):
        """缺少 skill_id 或 jira_url 时返回 400 与缺失字段。"""
        missing_skill = self.client.post("/skill/run", json={"jira_url": "https://jira.example.com/browse/CALL-1"})
        missing_url = self.client.post("/skill/run", json={"skill_id": "jira-gate-1"})

        self.assertEqual(missing_skill.status_code, 400)
        self.assertEqual(missing_skill.get_json()["missing_fields"], ["skill_id"])
        self.assertEqual(missing_url.status_code, 400)
        self.assertEqual(missing_url.get_json()["missing_fields"], ["jira_url"])

    def test_submit_rejects_unknown_and_invalid_skill_id(self):
        """未知或非法 skill_id 分别返回 404 与 400。"""
        unknown = self.client.post(
            "/skill/run",
            json={"skill_id": "not-installed", "jira_url": "https://jira.example.com/browse/CALL-1"},
        )
        malformed = self.client.post(
            "/skill/run",
            json={"skill_id": "../etc", "jira_url": "https://jira.example.com/browse/CALL-1"},
        )

        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(malformed.status_code, 404)

    def test_submit_rejects_non_http_url_or_host_outside_allowlist(self):
        """jira_url 必须是 http(s) 地址，且命中配置的主机白名单。"""
        invalid = self.client.post(
            "/skill/run",
            json={"skill_id": "jira-gate-1", "jira_url": "file:///etc/passwd"},
        )
        self.assertEqual(invalid.status_code, 400)

        restricted_app = create_test_app(SKILL_URL_ALLOWED_HOSTS="jira.in.wezhuiyi.com")
        with restricted_app.app_context():
            from extensions import db

            db.create_all()
            blocked = restricted_app.test_client().post(
                "/skill/run",
                json={"skill_id": "jira-gate-1", "jira_url": "https://evil.example.com/browse/CALL-1"},
            )

        self.assertEqual(blocked.status_code, 400)
        message = blocked.get_json()["error"]
        self.assertIn("白名单", message)
        self.assertIn("jira.in.wezhuiyi.com", message)
        self.assertIn("JIRA_BASE_URL=", message)

    def test_submit_creates_record_and_returns_status_url(self):
        """提交成功时创建排队记录并返回查询地址。"""
        with patch("app.skillrun.routes.routes.run_skill_task") as task:
            task.apply_async.return_value = MagicMock(id="SKL-TEST")
            response = self.client.post(
                "/skill/run",
                json={
                    "skill_id": "jira-gate-1",
                    "jira_url": "https://jira.in.wezhuiyi.com/browse/CALL-1446",
                    "inputs": {"review_round": 2},
                },
            )

        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertEqual(payload["skill_id"], "jira-gate-1")
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["status_url"], f"/skill/task/{payload['task_id']}")

        from app.skillrun import store

        record = store.find_record(payload["task_id"])
        self.assertIsNotNone(record)
        self.assertEqual(record.jira_url, "https://jira.in.wezhuiyi.com/browse/CALL-1446")
        self.assertEqual(store.load_json(record.inputs, {}), {"review_round": 2})
        task.apply_async.assert_called_once()
        self.assertEqual(task.apply_async.call_args.kwargs["queue"], "skill")

    def test_submit_reports_enqueue_failure(self):
        """入队失败时记录失败状态并返回 503。"""
        with patch("app.skillrun.routes.routes.run_skill_task") as task:
            task.apply_async.side_effect = RuntimeError("broker down")
            response = self.client.post(
                "/skill/run",
                json={"skill_id": "jira-gate-1", "jira_url": "https://jira.in.wezhuiyi.com/browse/CALL-1"},
            )

        self.assertEqual(response.status_code, 503)
        from app.skillrun import store

        record = store.find_record(response.get_json()["task_id"])
        self.assertEqual(record.status, "failed")
        self.assertIn("broker down", record.error_message)

    def _jira_issue_created_payload(self, key="CALL-1446", issue_type="缺陷"):
        """构造 Jira 原生 issue created webhook 的最小请求体。"""
        return {
            "webhookEvent": "jira:issue_created",
            "issue": {
                "key": key,
                "fields": {"issuetype": {"name": issue_type}},
            },
        }

    def test_jira_webhook_enqueues_bug_gate_with_api_token(self):
        """Jira 事件携带接口令牌时自动提交 jira-gate-bug。"""
        with patch("app.skillrun.routes.routes.run_skill_task") as task:
            response = self.client.post(
                "/skill/jira/events",
                headers={"X-API-Token": "skill-secret"},
                json=self._jira_issue_created_payload(),
            )

        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertEqual(payload["skill_id"], "jira-gate-bug")
        self.assertEqual(payload["issue_key"], "CALL-1446")
        self.assertFalse(payload["deduplicated"])
        task.apply_async.assert_called_once()

        from app.skillrun import store

        record = store.find_record(payload["task_id"])
        self.assertEqual(record.jira_url, "https://jira.example.com/browse/CALL-1446")
        self.assertEqual(record.requested_by, "jira-webhook")

    def test_jira_webhook_accepts_hmac_signature(self):
        """Jira 原生 webhook 可用 HMAC-SHA256 签名完成鉴权。"""
        self.app.config["JIRA_WEBHOOK_SECRET"] = "webhook-secret"
        body = json.dumps(self._jira_issue_created_payload("CALL-1447")).encode("utf-8")
        signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()

        with patch("app.skillrun.routes.routes.run_skill_task"):
            response = self.client.post(
                "/skill/jira/events",
                data=body,
                content_type="application/json",
                headers={"X-Hub-Signature-256": f"sha256={signature}"},
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["issue_key"], "CALL-1447")

    def test_jira_webhook_rejects_missing_or_wrong_credentials(self):
        """没有令牌或签名时不得触发技能任务。"""
        missing = self.client.post("/skill/jira/events", json=self._jira_issue_created_payload())
        wrong = self.client.post(
            "/skill/jira/events",
            headers={"X-API-Token": "wrong"},
            json=self._jira_issue_created_payload(),
        )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)

    def test_jira_webhook_ignores_updates_and_non_bug_types(self):
        """更新事件和非缺陷类型直接忽略，避免评论回写触发循环。"""
        update_payload = self._jira_issue_created_payload()
        update_payload["webhookEvent"] = "jira:issue_updated"
        update = self.client.post(
            "/skill/jira/events",
            headers={"X-API-Token": "skill-secret"},
            json=update_payload,
        )
        task = self.client.post(
            "/skill/jira/events",
            headers={"X-API-Token": "skill-secret"},
            json=self._jira_issue_created_payload("CALL-1448", "任务"),
        )

        self.assertEqual(update.status_code, 202)
        self.assertEqual(update.get_json()["reason"], "event_not_allowed")
        self.assertEqual(task.status_code, 202)
        self.assertEqual(task.get_json()["reason"], "issue_type_not_allowed")

    def test_jira_webhook_deduplicates_active_issue(self):
        """同一缺陷已有排队或执行中的任务时，webhook 重试返回原任务。"""
        payload = self._jira_issue_created_payload("CALL-1449")
        with patch("app.skillrun.routes.routes.run_skill_task") as task:
            first = self.client.post(
                "/skill/jira/events", headers={"X-API-Token": "skill-secret"}, json=payload
            )
            second = self.client.post(
                "/skill/jira/events", headers={"X-API-Token": "skill-secret"}, json=payload
            )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.get_json()["task_id"], first.get_json()["task_id"])
        self.assertTrue(second.get_json()["deduplicated"])
        task.apply_async.assert_called_once()

    def test_get_task_returns_record_and_handles_missing(self):
        """查询接口返回任务详情，未知任务返回 404。"""
        from app.skillrun import store

        record = store.create_record(
            skill_id="jira-gate-1",
            jira_url="https://jira.in.wezhuiyi.com/browse/CALL-2",
            requested_by="tester",
        )

        found = self.client.get(f"/skill/task/{record.task_id}")
        missing = self.client.get("/skill/task/SKL-NOT-EXISTS")

        self.assertEqual(found.status_code, 200)
        self.assertEqual(found.get_json()["requested_by"], "tester")
        self.assertEqual(found.get_json()["status"], "queued")
        self.assertEqual(missing.status_code, 404)

    def test_list_tasks_supports_filters(self):
        """任务列表接口按技能与状态过滤。"""
        from app.skillrun import store

        store.create_record(skill_id="jira-gate-1", jira_url="https://jira.in.wezhuiyi.com/browse/CALL-3")
        store.create_record(skill_id="other-skill", jira_url="https://jira.in.wezhuiyi.com/browse/CALL-4")

        response = self.client.get("/skill/tasks?skill_id=jira-gate-1")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["tasks"][0]["skill_id"], "jira-gate-1")

    def test_cancel_task_marks_record_cancelled(self):
        """取消接口撤销 Celery 任务并把记录置为已取消。"""
        from app.skillrun import store

        record = store.create_record(skill_id="jira-gate-1", jira_url="https://jira.in.wezhuiyi.com/browse/CALL-5")

        with patch("app.skillrun.routes.routes.celery") as celery_mock:
            response = self.client.post(f"/skill/task/{record.task_id}/cancel")

        self.assertEqual(response.status_code, 202)
        celery_mock.control.revoke.assert_called_once()
        self.assertEqual(store.find_record(record.task_id).status, "cancelled")


class ExternalApiTokenTests(unittest.TestCase):
    def build_app(self, token):
        """构造带外部接口令牌的最小应用。"""
        from flask import Flask, g, jsonify

        from app.auth.middleware import install_authentication

        app = Flask(__name__)
        app.config["SKILL_API_TOKEN"] = token
        app.config["SKILL_API_TOKEN_PATHS"] = "/skill"
        app.config["SKILL_API_USERNAME"] = "external-api"
        session_service = MagicMock()
        session_service.authenticate.return_value = None
        install_authentication(app, session_service)

        @app.post("/skill/run")
        def skill_run():
            return jsonify({"operator": getattr(g.current_user, "username", None)})

        @app.get("/business/data")
        def business():
            return jsonify({"ok": True})

        return app

    def test_external_token_grants_access_to_skill_endpoints(self):
        """携带正确的外部令牌可访问技能接口，并记录调用方标识。"""
        client = self.build_app("skill-secret").test_client()

        response = client.post("/skill/run", headers={"X-API-Token": "skill-secret"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["operator"], "external-api")

    def test_external_token_does_not_grant_access_to_other_endpoints(self):
        """外部令牌只对配置的路径生效，其他业务接口仍需登录。"""
        client = self.build_app("skill-secret").test_client()

        response = client.get("/business/data", headers={"X-API-Token": "skill-secret"})

        self.assertEqual(response.status_code, 401)

    def test_wrong_external_token_is_rejected(self):
        """令牌错误时按未登录处理。"""
        client = self.build_app("skill-secret").test_client()

        response = client.post("/skill/run", headers={"X-API-Token": "wrong"})

        self.assertEqual(response.status_code, 401)

    def test_external_token_disabled_when_not_configured(self):
        """未配置外部令牌时不接受任何 X-API-Token。"""
        client = self.build_app("").test_client()

        response = client.post("/skill/run", headers={"X-API-Token": "anything"})

        self.assertEqual(response.status_code, 401)
