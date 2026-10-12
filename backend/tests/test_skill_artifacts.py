"""技能交付物测试：任务详情返回文件清单，交付物按文件名下载且不能越出工作目录。"""
import sys
import tempfile
import unittest
from pathlib import Path
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

    config = with_locked_skill_env(
        {
            "TESTING": True,
            "AUTH_TEST_BYPASS": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "SKILLS_DIR": str(SKILLS_DIR),
            "SKILL_WORKSPACE_DIR": tempfile.gettempdir(),
            "SKILL_URL_ALLOWED_HOSTS": "",
        }
    )
    config.update(overrides)
    return create_app(config_overrides=config)


class ArtifactUnitTests(unittest.TestCase):
    """artifacts 模块的纯文件系统行为，不经过 HTTP。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "SKL-TEST"
        (self.workspace / "outputs").mkdir(parents=True)
        (self.workspace / "reports" / ".work" / "run-1").mkdir(parents=True)

        # 交付物：打包产物与报告
        self.zip = self.workspace / "outputs" / "CALL-1941-G1预检包-20261012-153418.zip"
        self.zip.write_bytes(b"PK\x03\x04payload")
        self.report = self.workspace / "reports" / "review-report-20261012.md"
        self.report.write_text("# 报告", encoding="utf-8")

        # 非交付物：中间草稿、work/ 下的附件解析产物、隐藏文件
        (self.workspace / "reports" / ".work" / "run-1" / "raw.json").write_text("{}", encoding="utf-8")
        (self.workspace / "work" / "jira-gate" / "CALL-1941" / "attachments").mkdir(parents=True)
        (self.workspace / "work" / "jira-gate" / "CALL-1941" / "attachments" / "需求.rar").write_bytes(b"rar")
        (self.workspace / "outputs" / ".hidden").write_text("x", encoding="utf-8")

    def test_lists_only_top_level_deliverables(self):
        """只列 outputs/ 与 reports/ 一层的文件，不列中间草稿、work/ 与隐藏文件。"""
        from app.skillrun import artifacts

        names = [item["name"] for item in artifacts.list_artifacts(str(self.workspace))]

        self.assertEqual(names, ["CALL-1941-G1预检包-20261012-153418.zip", "review-report-20261012.md"])

    def test_artifact_entry_carries_size_and_mtime(self):
        """清单项带体积与修改时间，供调用方判断产物是否完整。"""
        from app.skillrun import artifacts

        entry = next(
            item for item in artifacts.list_artifacts(str(self.workspace)) if item["name"].endswith(".zip")
        )

        self.assertEqual(entry["size"], self.zip.stat().st_size)
        self.assertTrue(entry["modified_at"].endswith("Z"), entry["modified_at"])

    def test_download_url_encodes_filename(self):
        """下载地址指向交付物接口，中文文件名按 URL 编码。"""
        from app.skillrun import artifacts

        items = artifacts.list_artifacts_with_urls("SKL-TEST", str(self.workspace))
        target = next(item for item in items if item["name"].endswith(".zip"))

        self.assertEqual(target["download_url"], "/skill/task/SKL-TEST/artifacts/CALL-1941-G1%E9%A2%84%E6%A3%80%E5%8C%85-20261012-153418.zip")

    def test_download_url_carries_ticket_when_signer_given(self):
        """传入签名器时下载地址带上短时效票据，浏览器可直接打开下载。"""
        from app.skillrun import artifacts

        items = artifacts.list_artifacts_with_urls(
            "SKL-TEST",
            str(self.workspace),
            ticket_signer=lambda task_id, name: f"ticket-for-{task_id}-{len(name)}",
        )
        target = next(item for item in items if item["name"].endswith(".zip"))

        self.assertEqual(
            target["download_url"],
            "/skill/task/SKL-TEST/artifacts/CALL-1941-G1%E9%A2%84%E6%A3%80%E5%8C%85-20261012-153418.zip?ticket=ticket-for-SKL-TEST-35",
        )

    def test_missing_workspace_returns_empty_list(self):
        """工作目录不存在或未登记时返回空清单，不抛异常。"""
        from app.skillrun import artifacts

        self.assertEqual(artifacts.list_artifacts(None), [])
        self.assertEqual(artifacts.list_artifacts(str(self.workspace / "nope")), [])

    def test_resolve_rejects_hostile_names(self):
        """带分隔符、.. 或隐藏文件名一律拒绝，避免读到工作目录之外的文件。"""
        from app.skillrun import artifacts

        for bad in ("../secret.txt", "..\\..\\secret.txt", "sub/dir.txt", "..", ".hidden", "", "   "):
            with self.subTest(name=bad):
                with self.assertRaises(artifacts.ArtifactError) as caught:
                    artifacts.resolve_artifact(str(self.workspace), bad)
                self.assertEqual(caught.exception.reason, "invalid-name")

    def test_resolve_rejects_file_outside_artifact_dirs(self):
        """只认交付物目录里的文件：work/ 下的附件解析产物按不存在处理。"""
        from app.skillrun import artifacts

        with self.assertRaises(artifacts.ArtifactError) as caught:
            artifacts.resolve_artifact(str(self.workspace), "需求.rar")

        self.assertEqual(caught.exception.reason, "not-found")

    def test_resolve_returns_real_path_for_deliverable(self):
        """合法文件名解析为交付物目录内的真实路径。"""
        from app.skillrun import artifacts

        resolved = artifacts.resolve_artifact(str(self.workspace), self.zip.name)

        self.assertEqual(Path(resolved), self.zip.resolve())


class ArtifactRouteTests(unittest.TestCase):
    """接口层：任务详情给出文件名，交付物接口按文件名下载。"""

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

    def create_record_with_workspace(self, files=None):
        """建立一条带工作目录的任务记录，并按需在其中落交付物。"""
        from app.skillrun import store

        record = store.create_record(skill_id="jira-gate-1-mock", jira_url="https://jira.example.com/browse/CALL-1")
        workspace = Path(self.tmp.name) / record.task_id
        (workspace / "outputs").mkdir(parents=True, exist_ok=True)
        for name, content in (files or {}).items():
            (workspace / "outputs" / name).write_bytes(content)
        store.mark_running(record, str(workspace))
        return record

    def get_task_detail(self, task_id):
        """查询任务详情；Celery 状态用 mock 掉，避免用例依赖 Redis。"""
        with patch("app.skillrun.routes.routes.AsyncResult") as async_result:
            async_result.return_value.state = "SUCCESS"
            async_result.return_value.info = None
            return self.client.get(f"/skill/task/{task_id}")

    def test_task_detail_lists_artifacts_with_download_url(self):
        """任务详情回出交付物清单与下载地址，调用方据此取文件名。"""
        content = b"PK\x03\x04zip"
        record = self.create_record_with_workspace({"CALL-1-G1预检包.zip": content})

        payload = self.get_task_detail(record.task_id).get_json()

        self.assertEqual(len(payload["artifacts"]), 1)
        artifact = payload["artifacts"][0]
        self.assertEqual(artifact["name"], "CALL-1-G1预检包.zip")
        self.assertEqual(artifact["size"], len(content))
        # 地址末尾带短时效签名票据（内容随机，这里只校验前缀与文件名编码），浏览器可以直接打开。
        self.assertTrue(
            artifact["download_url"].startswith(
                f"/skill/task/{record.task_id}/artifacts/CALL-1-G1%E9%A2%84%E6%A3%80%E5%8C%85.zip?ticket="
            ),
            artifact["download_url"],
        )

    def test_task_detail_artifacts_empty_without_workspace(self):
        """任务还没跑起来（没有工作目录）时清单为空，不报错。"""
        from app.skillrun import store

        record = store.create_record(skill_id="jira-gate-1-mock", jira_url="https://jira.example.com/browse/CALL-1")

        response = self.get_task_detail(record.task_id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["artifacts"], [])

    def test_download_returns_file_with_chinese_filename(self):
        """交付物接口返回文件内容，并按附件下载保留中文原名。"""
        record = self.create_record_with_workspace({"CALL-1-G1预检包-20261012.zip": b"PK\x03\x04zip-bytes"})

        response = self.client.get(f"/skill/task/{record.task_id}/artifacts/CALL-1-G1预检包-20261012.zip")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"PK\x03\x04zip-bytes")
        disposition = response.headers["Content-Disposition"]
        self.assertIn("attachment", disposition)
        self.assertIn(".zip", disposition)

    def test_download_unknown_task_returns_404(self):
        """任务不存在时返回 404。"""
        response = self.client.get("/skill/task/SKL-NOT-EXISTS/artifacts/any.zip")

        self.assertEqual(response.status_code, 404)

    def test_download_unknown_artifact_returns_404(self):
        """交付物不存在时返回 404 并给出机器可读原因。"""
        record = self.create_record_with_workspace({"real.zip": b"zip"})

        response = self.client.get(f"/skill/task/{record.task_id}/artifacts/nope.zip")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["reason"], "not-found")

    def test_download_does_not_escape_workspace(self):
        """越界尝试不能读到工作目录之外的文件，也不能返回 200。"""
        record = self.create_record_with_workspace({"real.zip": b"zip"})
        secret = Path(self.tmp.name) / "secret.txt"
        secret.write_text("top-secret", encoding="utf-8")

        for attempt in (
            f"/skill/task/{record.task_id}/artifacts/../secret.txt",
            f"/skill/task/{record.task_id}/artifacts/%2e%2e%2fsecret.txt",
            f"/skill/task/{record.task_id}/artifacts/..%5Csecret.txt",
        ):
            with self.subTest(url=attempt):
                response = self.client.get(attempt)
                self.assertNotEqual(response.status_code, 200)
                self.assertNotIn(b"top-secret", response.data)

    def test_download_skips_intermediate_work_files(self):
        """work/ 下的附件解析产物不是交付物，不能被下载。"""
        record = self.create_record_with_workspace({"real.zip": b"zip"})
        work_dir = Path(self.tmp.name) / record.task_id / "work" / "jira-gate" / "CALL-1"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "comment.txt").write_text("草稿", encoding="utf-8")

        response = self.client.get(f"/skill/task/{record.task_id}/artifacts/comment.txt")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["reason"], "not-found")


if __name__ == "__main__":
    unittest.main()
