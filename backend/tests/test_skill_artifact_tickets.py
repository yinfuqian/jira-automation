"""交付物下载票据测试。

目标：任务详情里的 download_url 能直接在浏览器打开（地址栏、超链接都带不了请求头），
同时过期票据、换文件/换任务复用、内容篡改与错误密钥都会被拒绝，不下发文件内容。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlsplit


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

try:
    from tests.skill_env_fixture import with_locked_skill_env
except ModuleNotFoundError:  # 兼容以 tests 目录为根直接运行
    from skill_env_fixture import with_locked_skill_env

PROJECT_ROOT = BACKEND_DIR.parent
SKILLS_DIR = PROJECT_ROOT / "skills"
# 测试用密钥与令牌：刻意配成不同值，验证票据签名不依赖 SKILL_API_TOKEN。
TEST_TICKET_SECRET = "ticket-secret-for-test"
TEST_API_TOKEN = "skill-api-token-for-test"


class ConfigStub:
    """只提供 config 字典的最小替身，让票据单元用例不必依赖 Flask 应用。"""

    def __init__(self, **config):
        self.config = config


def create_test_app(**overrides):
    """构造开启真实鉴权（不跳过登录校验）的测试应用。"""
    from app import create_app

    config = with_locked_skill_env(
        {
            "TESTING": True,
            "AUTH_TEST_BYPASS": False,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "SKILLS_DIR": str(SKILLS_DIR),
            "SKILL_WORKSPACE_DIR": tempfile.gettempdir(),
            "SKILL_URL_ALLOWED_HOSTS": "",
            "SKILL_API_TOKEN": TEST_API_TOKEN,
            "SKILL_ARTIFACT_TICKET_SECRET": TEST_TICKET_SECRET,
        }
    )
    config.update(overrides)
    return create_app(config_overrides=config)


def artifact_path(task_id, name):
    """拼出交付物下载路径，文件名按 URL 编码。"""
    return f"/skill/task/{task_id}/artifacts/{quote(name, safe='')}"


def artifact_path_decoded(task_id, name):
    """拼出「已被 Werkzeug 解码」的下载路径，用于直接调用票据模块的单元用例。"""
    return f"/skill/task/{task_id}/artifacts/{name}"


def ticket_of(download_url):
    """从 download_url 里取出票据。"""
    values = parse_qs(urlsplit(download_url).query).get("ticket") or [""]
    return values[0]


class TicketUnitTests(unittest.TestCase):
    """tickets 模块的纯签名行为，不经过 HTTP。"""

    def setUp(self):
        from app.skillrun import tickets

        self.tickets = tickets
        self.secret = TEST_TICKET_SECRET

    def test_ticket_round_trip(self):
        """自己签发的票据可以校验通过。"""
        ticket = self.tickets.issue_ticket(self.secret, "SKL-1", "包.zip")

        self.assertTrue(self.tickets.verify_ticket(self.secret, ticket, "SKL-1", "包.zip", 1800))

    def test_ticket_is_bound_to_task_and_filename(self):
        """票据与「任务 + 文件名」绑定，换个任务或换个文件都不生效。"""
        ticket = self.tickets.issue_ticket(self.secret, "SKL-1", "包.zip")

        self.assertFalse(self.tickets.verify_ticket(self.secret, ticket, "SKL-2", "包.zip", 1800))
        self.assertFalse(self.tickets.verify_ticket(self.secret, ticket, "SKL-1", "别的包.zip", 1800))

    def test_ticket_rejects_tampering_and_other_secret(self):
        """票据被改动或换了签名密钥都校验失败。"""
        ticket = self.tickets.issue_ticket(self.secret, "SKL-1", "包.zip")

        self.assertFalse(self.tickets.verify_ticket(self.secret, ticket + "x", "SKL-1", "包.zip", 1800))
        self.assertFalse(self.tickets.verify_ticket("other-secret", ticket, "SKL-1", "包.zip", 1800))

    def test_ticket_expires(self):
        """超过有效期后票据作废。"""
        ticket = self.tickets.issue_ticket(self.secret, "SKL-1", "包.zip")

        # 用负的有效期模拟过期：签发与校验在同一秒时，max_age=0 的 age 恰好是 0，不会判过期。
        self.assertFalse(self.tickets.verify_ticket(self.secret, ticket, "SKL-1", "包.zip", -1))

    def test_missing_secret_or_ticket_never_passes(self):
        """没配密钥或没带票据时一律不放行，避免退化成匿名下载。"""
        ticket = self.tickets.issue_ticket(self.secret, "SKL-1", "包.zip")

        self.assertFalse(self.tickets.verify_ticket("", ticket, "SKL-1", "包.zip", 1800))
        self.assertFalse(self.tickets.verify_ticket(self.secret, "", "SKL-1", "包.zip", 1800))

    def test_resolve_download_scope_only_matches_artifact_paths(self):
        """只有交付物下载路径能解出票据作用域，带子目录的越界路径不会匹配。"""
        self.assertEqual(self.tickets.resolve_download_scope(artifact_path_decoded("SKL-1", "包.zip")), ("SKL-1", "包.zip"))
        self.assertIsNone(self.tickets.resolve_download_scope(artifact_path_decoded("SKL-1", "sub/dir.zip")))
        self.assertIsNone(self.tickets.resolve_download_scope("/skill/task/SKL-1"))
        self.assertIsNone(self.tickets.resolve_download_scope("/skill/tasks"))

    def test_sign_download_url_falls_back_to_api_token_and_gives_up_without_secret(self):
        """签名密钥缺省回退接口令牌；两者都没有时不发票据（地址只能用请求头下载）。"""
        fallback = ConfigStub(SKILL_API_TOKEN=TEST_API_TOKEN)
        without_secret = ConfigStub(SKILL_API_TOKEN="")

        self.assertTrue(self.tickets.sign_download_url(fallback, "SKL-1", "包.zip"))
        self.assertTrue(self.tickets.sign_download_url(ConfigStub(SKILL_ARTIFACT_TICKET_SECRET="s"), "SKL-1", "包.zip"))
        self.assertIsNone(self.tickets.sign_download_url(without_secret, "SKL-1", "包.zip"))

    def test_verify_download_ticket_returns_principal_for_matching_path(self):
        """中间件回调只在路径与票据都匹配时返回调用方标识。"""
        app = ConfigStub(SKILL_ARTIFACT_TICKET_SECRET=self.secret, SKILL_ARTIFACT_TICKET_TTL=1800)
        ticket = self.tickets.issue_ticket(self.secret, "SKL-1", "包.zip")

        self.assertEqual(self.tickets.verify_download_ticket(app, artifact_path_decoded("SKL-1", "包.zip"), ticket), "external-api")
        self.assertIsNone(self.tickets.verify_download_ticket(app, artifact_path_decoded("SKL-2", "包.zip"), ticket))
        self.assertIsNone(self.tickets.verify_download_ticket(app, "/skill/task/SKL-1", ticket))


class TicketRouteTests(unittest.TestCase):
    """接口层：带票据的 download_url 无需请求头即可下载，伪造票据拿不到内容。"""

    def setUp(self):
        from extensions import db

        self.app = create_test_app()
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.addCleanup(self._cleanup_context)
        self.client = self.app.test_client()

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _cleanup_context(self):
        from extensions import db

        db.session.remove()
        self.context.pop()

    def create_record_with_workspace(self, files):
        """建立一条带工作目录与交付物的任务记录。"""
        from app.skillrun import store

        record = store.create_record(skill_id="jira-gate-1-mock", jira_url="https://jira.example.com/browse/CALL-1")
        workspace = Path(self.tmp.name) / record.task_id
        (workspace / "outputs").mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (workspace / "outputs" / name).write_bytes(content)
        store.mark_running(record, str(workspace))
        return record

    def get_task_detail(self, task_id):
        """用接口令牌查详情；Celery 状态 mock 掉，避免用例依赖 Redis。"""
        with patch("app.skillrun.routes.routes.AsyncResult") as async_result:
            async_result.return_value.state = "SUCCESS"
            async_result.return_value.info = None
            return self.client.get(f"/skill/task/{task_id}", headers={"X-API-Token": TEST_API_TOKEN})

    def detail_artifact(self, record, name):
        """取回详情里指定交付物的清单项。"""
        payload = self.get_task_detail(record.task_id).get_json()
        return next(item for item in payload["artifacts"] if item["name"] == name)

    def test_download_url_opens_directly_in_browser(self):
        """详情给出的 download_url 不带任何请求头就能下载到文件本身。"""
        content = b"PK\x03\x04zip-bytes"
        record = self.create_record_with_workspace({"CALL-1-G1预检包.zip": content})

        artifact = self.detail_artifact(record, "CALL-1-G1预检包.zip")
        self.assertTrue(ticket_of(artifact["download_url"]), artifact["download_url"])

        response = self.client.get(artifact["download_url"])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, content)
        self.assertIn("attachment", response.headers["Content-Disposition"])

    def test_download_without_ticket_or_token_is_rejected(self):
        """没有票据也没有令牌时接口保持 401，票据是唯一放行条件。"""
        record = self.create_record_with_workspace({"CALL-1-G1预检包.zip": b"zip"})

        response = self.client.get(artifact_path(record.task_id, "CALL-1-G1预检包.zip"))

        self.assertEqual(response.status_code, 401)

    def test_ticket_for_another_file_is_rejected(self):
        """一个文件的票据不能拿去下载同任务下的另一个文件。"""
        record = self.create_record_with_workspace({"a.zip": b"a-bytes", "b.zip": b"b-bytes"})

        stolen = ticket_of(self.detail_artifact(record, "a.zip")["download_url"])
        response = self.client.get(f"{artifact_path(record.task_id, 'b.zip')}?ticket={stolen}")

        self.assertEqual(response.status_code, 401)
        self.assertNotIn(b"b-bytes", response.data)

    def test_tampered_ticket_is_rejected(self):
        """被改动的票据无效。"""
        record = self.create_record_with_workspace({"a.zip": b"a-bytes"})

        stolen = ticket_of(self.detail_artifact(record, "a.zip")["download_url"])
        response = self.client.get(f"{artifact_path(record.task_id, 'a.zip')}?ticket={stolen}tampered")

        self.assertEqual(response.status_code, 401)

    def test_expired_ticket_is_rejected(self):
        """票据过期后同样拿不到文件（这里把有效期临时压成负数模拟过期）。"""
        record = self.create_record_with_workspace({"a.zip": b"a-bytes"})
        url = self.detail_artifact(record, "a.zip")["download_url"]

        with patch("app.skillrun.tickets.ticket_ttl", return_value=-1):
            response = self.client.get(url)

        self.assertEqual(response.status_code, 401)

    def test_api_token_still_works_without_ticket(self):
        """没有签名密钥的场景仍可用接口令牌下载，原有调用方式不被破坏。"""
        record = self.create_record_with_workspace({"a.zip": b"a-bytes"})

        response = self.client.get(
            artifact_path(record.task_id, "a.zip"),
            headers={"X-API-Token": TEST_API_TOKEN},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"a-bytes")


if __name__ == "__main__":
    unittest.main()
