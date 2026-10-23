# 技能调用统计测试：覆盖排队、结果文件、最终消息标记、失败、取消、回收与子技能不计数。
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

try:
    from tests.skill_env_fixture import with_locked_skill_env
except ModuleNotFoundError:
    from skill_env_fixture import with_locked_skill_env

PROJECT_ROOT = BACKEND_DIR.parent
SKILLS_DIR = PROJECT_ROOT / "skills"


def create_test_app(**overrides):
    # 构造使用内存数据库的测试应用。
    from app import create_app

    config = {
        "TESTING": True,
        "AUTH_TEST_BYPASS": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SKILLS_DIR": str(SKILLS_DIR),
        "SKILL_WORKSPACE_DIR": tempfile.gettempdir(),
    }
    config = with_locked_skill_env(config)
    config.update(overrides)
    return create_app(config_overrides=config)


class SkillUsageRecordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 建立类级应用与内存表。
        from extensions import db

        cls.app = create_test_app()
        cls.context = cls.app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        # 清理会话与数据库连接。
        from extensions import db

        db.session.remove()
        db.engine.dispose()
        cls.context.pop()

    def setUp(self):
        # 每个用例使用独立工作目录并清空两类记录。
        from app.skillrun.models.model import SkillRunRecord
        from app.skillrun.models.usage import SkillCallRecord
        from extensions import db

        db.session.query(SkillCallRecord).delete()
        db.session.query(SkillRunRecord).delete()
        db.session.commit()
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.app.config["SKILL_WORKSPACE_DIR"] = self.workspace.name

    def new_record(self, trigger_source="api", skill_id="jira-gate-1"):
        # 创建排队任务并返回原任务记录。
        from app.skillrun import store

        return store.create_record(
            skill_id=skill_id,
            jira_url="https://jira.in.wezhuiyi.com/browse/CALL-100",
            requested_by="tester",
            trigger_source=trigger_source,
        )

    def call_of(self, record):
        # 读取原任务对应的统计行。
        from app.skillrun.models.usage import SkillCallRecord

        return SkillCallRecord.query.filter_by(record_id=record.id).one()

    def write_result(self, record, payload):
        # 在任务根目录写机器可读结果文件。
        path = Path(self.workspace.name) / record.skill_id / record.task_id / ".skill-result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def run_task(self, record, run_result):
        # mock Codex 子进程并同步执行任务。
        from app.skillrun.tasks import run_skill_task
        from extensions import db

        with patch("app.skillrun.tasks.codex_runner.run_codex_skill", return_value=run_result):
            result = run_skill_task.apply(args=[record.id]).get()
        db.session.expire_all()
        return result

    def test_create_record_inserts_queued_call(self):
        # 任务创建后立即出现一条 queued 统计行，并记录来源与单号。
        record = self.new_record(trigger_source="webhook")
        call = self.call_of(record)

        self.assertEqual(call.execution_status, "queued")
        self.assertEqual(call.outcome, "unknown")
        self.assertEqual(call.conclusion_source, "none")
        self.assertEqual(call.trigger_source, "webhook")
        self.assertEqual(call.issue_key, "CALL-100")
        self.assertIsNotNone(call.created_at)

    def test_result_file_maps_passed_and_removes_file(self):
        # 结果文件写通过结论时，执行状态与业务结论分开记录，并在入库后删除文件。
        from app.skillrun.codex_runner import CodexRunResult

        record = self.new_record()
        path = self.write_result(record, {
            "schemaVersion": 1,
            "skillId": "jira-gate-1",
            "issueKey": "CALL-100",
            "conclusion": "通过",
            "summary": "G1 与 QA 均通过",
            "blockingCount": 0,
        })
        self.run_task(record, CodexRunResult(exit_code=0, last_message="完成", duration_ms=321))
        call = self.call_of(record)

        self.assertEqual(call.execution_status, "succeeded")
        self.assertEqual(call.outcome, "passed")
        self.assertEqual(call.conclusion_text, "通过")
        self.assertEqual(call.conclusion_source, "file")
        self.assertEqual(call.blocking_count, 0)
        self.assertEqual(call.duration_ms, 321)
        self.assertEqual(call.summary, "G1 与 QA 均通过")
        self.assertFalse(path.exists())

    def test_rejected_conclusion_does_not_change_execution_status(self):
        # 业务退回仍是正常执行结束，不能把 succeeded 当成业务通过。
        from app.skillrun.codex_runner import CodexRunResult

        record = self.new_record()
        self.write_result(record, {
            "schemaVersion": 1,
            "skillId": "jira-gate-1",
            "conclusion": "退回补充",
            "summary": "设计未达标",
            "blockingCount": 2,
        })
        self.run_task(record, CodexRunResult(exit_code=0, last_message="完成", duration_ms=100))
        call = self.call_of(record)

        self.assertEqual(call.execution_status, "succeeded")
        self.assertEqual(call.outcome, "rejected")
        self.assertEqual(call.blocking_count, 2)

    def test_marker_is_used_when_result_file_missing(self):
        # 没有结果文件时，从 last-message.md 的标记读取结论。
        from app.skillrun.codex_runner import CodexRunResult

        record = self.new_record()
        marker_dir = Path(self.workspace.name) / record.skill_id / record.task_id
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker = {
            "schemaVersion": 1,
            "skillId": "jira-gate-1",
            "conclusion": "通过",
            "summary": "标记结论",
            "blockingCount": 0,
        }
        (marker_dir / "last-message.md").write_text(
            "最终结论如下" + chr(10) + "SKILL_RESULT_JSON " + json.dumps(marker, ensure_ascii=False) + chr(10),
            encoding="utf-8",
        )
        self.run_task(record, CodexRunResult(exit_code=0, last_message="完成", duration_ms=100))
        call = self.call_of(record)

        self.assertEqual(call.outcome, "passed")
        self.assertEqual(call.conclusion_source, "marker")

    def test_unmapped_conclusion_is_invalid(self):
        # 无法映射的结论保留原文，但结论来源记为 invalid。
        from app.skillrun.codex_runner import CodexRunResult

        record = self.new_record()
        self.write_result(record, {
            "schemaVersion": 1,
            "skillId": "jira-gate-1",
            "conclusion": "再看看",
            "summary": "人工确认",
        })
        self.run_task(record, CodexRunResult(exit_code=0, last_message="完成", duration_ms=100))
        call = self.call_of(record)

        self.assertEqual(call.outcome, "unknown")
        self.assertEqual(call.conclusion_source, "invalid")
        self.assertEqual(call.conclusion_text, "再看看")

    def test_skill_specific_conclusions_are_mapped(self):
        # 缺陷门禁与代码交付使用各自的中文结论，也需要映射为统一统计枚举。
        from app.skillrun.usage import map_conclusion

        cases = {
            "打回": "rejected",
            "缺分析参数": "rejected",
            "放行": "passed",
            "已自动分析": "passed",
            "映射缺失，已跳过": "unknown",
            "已中止": "aborted",
        }
        for conclusion, expected in cases.items():
            with self.subTest(conclusion=conclusion):
                self.assertEqual(map_conclusion(conclusion), expected)

    def test_timeout_sets_failure_kind(self):
        # 超时任务记录 timeout 与专用失败类型。
        from app.skillrun.codex_runner import CodexRunResult

        record = self.new_record()
        self.run_task(record, CodexRunResult(exit_code=None, timed_out=True, error="超时", duration_ms=1800000))
        call = self.call_of(record)

        self.assertEqual(call.execution_status, "timeout")
        self.assertEqual(call.failure_kind, "timeout")

    def test_env_missing_sets_failure_kind(self):
        # 运行环境缺失时不调用 Codex，统计行记录 env_missing。
        from app.skillrun.tasks import run_skill_task
        from extensions import db

        record = self.new_record()
        with patch("app.skillrun.tasks.env_lock.missing_env_keys", return_value=["JIRA_TOKEN"]):
            run_skill_task.apply(args=[record.id]).get()
        db.session.expire_all()
        call = self.call_of(record)

        self.assertEqual(call.execution_status, "failed")
        self.assertEqual(call.failure_kind, "env_missing")

    def test_cancel_marks_aborted(self):
        # 取消任务记录 cancelled 与 aborted，便于和业务不通过区分。
        from app.skillrun import store, usage
        from app.skillrun.models.model import SkillRunRecord

        record = self.new_record()
        store.mark_finished(
            record,
            SkillRunRecord.STATUS_CANCELLED,
            stage="cancelled",
            failure_kind=usage.FAILURE_CANCELLED,
            error_message="任务已请求取消",
        )
        call = self.call_of(record)

        self.assertEqual(call.execution_status, "cancelled")
        self.assertEqual(call.outcome, "aborted")
        self.assertEqual(call.failure_kind, "cancelled")

    def test_worker_recovery_marks_failure(self):
        # worker 重启回收孤儿任务时，统计行记录 failed 与 worker_restart。
        from app.skillrun import recovery, store

        record = self.new_record()
        workspace = Path(self.workspace.name) / record.skill_id / record.task_id
        store.mark_running(record, str(workspace))
        recovered = recovery.recover_orphan_tasks(active_record_ids=set())
        call = self.call_of(record)

        self.assertIn(record.task_id, recovered)
        self.assertEqual(call.execution_status, "failed")
        self.assertEqual(call.failure_kind, "worker_restart")
        self.assertEqual(call.outcome, "unknown")

    def test_gate_task_has_only_one_call_row(self):
        # 顶层门禁技能只产生一行统计，内部子技能不落统计表。
        from app.skillrun.codex_runner import CodexRunResult
        from app.skillrun.models.usage import SkillCallRecord

        record = self.new_record()
        self.write_result(record, {
            "schemaVersion": 1,
            "skillId": "jira-gate-1",
            "conclusion": "通过",
            "blockingCount": 0,
        })
        sub_dir = Path(self.workspace.name) / record.skill_id / record.task_id / "review-jira-qa"
        sub_dir.mkdir(parents=True, exist_ok=True)
        (sub_dir / "result.json").write_text(json.dumps({"conclusion": "通过"}, ensure_ascii=False), encoding="utf-8")
        self.run_task(record, CodexRunResult(exit_code=0, last_message="完成", duration_ms=100))

        self.assertEqual(SkillCallRecord.query.count(), 1)
        self.assertEqual(SkillCallRecord.query.filter(SkillCallRecord.skill_id != "jira-gate-1").count(), 0)

    def test_original_task_table_columns_unchanged(self):
        # 统计方案只新增表，不改原任务表字段。按预期字段集合做防回归校验。
        from app.skillrun.models.model import SkillRunRecord

        expected = {
            "id", "task_id", "skill_id", "jira_url", "inputs", "status", "stage",
            "progress", "result_text", "error_message", "codex_session_id",
            "workspace_dir", "requested_by", "exit_code", "duration_ms",
            "created_at", "started_at", "finished_at",
        }
        self.assertEqual(set(SkillRunRecord.__table__.columns.keys()), expected)
