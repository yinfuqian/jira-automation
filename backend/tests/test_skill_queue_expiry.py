"""技能队列过期测试：覆盖 Redis 消息移除、状态失败与统计同步。"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

PROJECT_ROOT = BACKEND_DIR.parent
SKILLS_DIR = PROJECT_ROOT / "skills"


def create_test_app(**overrides):
    """构造使用内存数据库的测试应用。"""
    from app import create_app

    config = {
        "TESTING": True,
        "AUTH_TEST_BYPASS": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SKILLS_DIR": str(SKILLS_DIR),
        "SKILL_WORKSPACE_DIR": tempfile.gettempdir(),
    }
    config.update(overrides)
    return create_app(config_overrides=config)


def celery_message(task_id: str, task_name: str = "skillrun.run_skill") -> bytes:
    """构造与 Celery 协议 v2 一致的队列消息。"""
    return json.dumps(
        {
            "headers": {"id": task_id, "task": task_name},
            "body": [[1], {}, {"callbacks": None}],
        }
    ).encode("utf-8")


class FakeRedis:
    """只实现队列清理所需 lrange/lrem 的最小 Redis 假实现。"""

    def __init__(self, messages=None):
        """按队列名保存消息，便于断言删除结果。"""
        self.queues = {"skill": list(messages or [])}
        self.lrange_calls = 0

    def lrange(self, key, start, end):
        """返回队列闭区间内容，与 redis-py 语义保持一致。"""
        self.lrange_calls += 1
        items = self.queues.get(key, [])
        if end == -1:
            return items[start:]
        return items[start : end + 1]

    def lrem(self, key, count, value):
        """从队列中删除匹配元素并返回删除数量。"""
        remaining = []
        removed = 0
        for item in self.queues.get(key, []):
            if item == value and removed < count:
                removed += 1
                continue
            remaining.append(item)
        self.queues[key] = remaining
        return removed


class SkillQueueExpiryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """创建类级应用与内存表，供各用例复用。"""
        from extensions import db

        cls.app = create_test_app()
        cls.context = cls.app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        """清理会话与数据库连接。"""
        from extensions import db

        db.session.remove()
        db.engine.dispose()
        cls.context.pop()

    def setUp(self):
        """清空任务与统计表，并准备一条排队记录。"""
        from app.skillrun import store
        from app.skillrun.models.model import SkillRunRecord
        from app.skillrun.models.usage import SkillCallRecord
        from extensions import db

        db.session.query(SkillCallRecord).delete()
        db.session.query(SkillRunRecord).delete()
        db.session.commit()
        self.record = store.create_record(
            skill_id="jira-gate-1",
            jira_url="https://jira.in.wezhuiyi.com/browse/CALL-1446",
        )

    def make_expired(self, record=None):
        """把记录的创建时间改到 61 分钟前。"""
        from app.skillrun import store

        return store.update_record(
            record or self.record,
            created_at=datetime.utcnow() - timedelta(minutes=61),
        )

    def test_expired_queued_task_is_removed_and_marked_failed(self):
        """过期任务从 Redis 移除，数据库与统计表同时记为失败。"""
        from app.skillrun import store
        from app.skillrun.models.usage import SkillCallRecord
        from app.skillrun.queue_expiry import expire_stale_queued_tasks

        self.make_expired()
        redis_client = FakeRedis([celery_message(self.record.task_id)])

        summary = expire_stale_queued_tasks(
            ttl_seconds=60 * 60,
            redis_client=redis_client,
        )

        record = store.find_record(self.record.task_id)
        self.assertEqual(summary["expired"], [self.record.task_id])
        self.assertEqual(summary["removedFromRedis"], 1)
        self.assertEqual(redis_client.queues["skill"], [])
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.stage, "queue_expired")
        self.assertIsNotNone(record.finished_at)
        self.assertIn("超过 60 分钟", record.error_message)

        call = SkillCallRecord.query.filter_by(record_id=record.id).one()
        self.assertEqual(call.execution_status, "failed")
        self.assertEqual(call.failure_kind, "queue_expired")
        self.assertEqual(call.outcome, "unknown")
        self.assertIn("超过 60 分钟", call.summary)

    def test_fresh_queued_task_is_kept(self):
        """未到 60 分钟的任务和 Redis 消息都必须保留。"""
        from app.skillrun import store
        from app.skillrun.queue_expiry import expire_stale_queued_tasks

        message = celery_message(self.record.task_id)
        redis_client = FakeRedis([message])

        summary = expire_stale_queued_tasks(
            ttl_seconds=60 * 60,
            redis_client=redis_client,
        )

        self.assertEqual(summary["expired"], [])
        self.assertEqual(redis_client.queues["skill"], [message])
        self.assertEqual(store.find_record(self.record.task_id).status, "queued")

    def test_multiple_expired_tasks_reuse_one_queue_snapshot(self):
        """多条过期任务只读取一次队列，避免积压时反复扫描 Redis List。"""
        from app.skillrun import store
        from app.skillrun.queue_expiry import expire_stale_queued_tasks

        second = store.create_record(
            skill_id="jira-gate-1",
            jira_url="https://jira.in.wezhuiyi.com/browse/CALL-1447",
        )
        self.make_expired()
        self.make_expired(second)
        redis_client = FakeRedis(
            [celery_message(self.record.task_id), celery_message(second.task_id)]
        )

        summary = expire_stale_queued_tasks(ttl_seconds=60 * 60, redis_client=redis_client)

        self.assertCountEqual(summary["expired"], [self.record.task_id, second.task_id])
        self.assertEqual(summary["removedFromRedis"], 2)
        self.assertEqual(redis_client.lrange_calls, 1)
        self.assertEqual(redis_client.queues["skill"], [])

    def test_missing_redis_message_still_closes_stale_record(self):
        """Redis 消息已丢失时也要收敛排队状态，避免任务永久卡在 queued。"""
        from app.skillrun import store
        from app.skillrun.queue_expiry import expire_stale_queued_tasks

        self.make_expired()

        summary = expire_stale_queued_tasks(ttl_seconds=60 * 60, redis_client=FakeRedis())

        self.assertEqual(summary["expired"], [self.record.task_id])
        self.assertEqual(summary["missingMessages"], 1)
        self.assertEqual(store.find_record(self.record.task_id).status, "failed")

    def test_running_task_is_not_touched(self):
        """只处理 queued 状态，执行中的任务不能被过期清理误伤。"""
        from app.skillrun import store
        from app.skillrun.queue_expiry import expire_stale_queued_tasks

        self.make_expired()
        store.mark_running(self.record, workspace_dir=tempfile.gettempdir())
        redis_client = FakeRedis([celery_message(self.record.task_id)])

        summary = expire_stale_queued_tasks(ttl_seconds=60 * 60, redis_client=redis_client)

        self.assertEqual(summary["expired"], [])
        self.assertEqual(store.find_record(self.record.task_id).status, "running")
        self.assertEqual(len(redis_client.queues["skill"]), 1)

    def test_worker_refuses_to_run_an_expired_queued_message(self):
        """即使消息被 worker 延迟取出，也不能执行已过期任务。"""
        from app.skillrun import store
        from app.skillrun.tasks import run_skill_task

        self.make_expired()

        with patch("app.skillrun.tasks.codex_runner.run_codex_skill") as runner:
            payload = run_skill_task.apply(args=[self.record.id]).get()

        runner.assert_not_called()
        record = store.find_record(self.record.task_id)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["stage"], "queue_expired")
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.stage, "queue_expired")

    def test_worker_skips_when_expiry_cleanup_loses_race(self):
        """状态已被其它清理流程改变时，worker 不得误报失败或继续执行。"""
        from app.skillrun.tasks import run_skill_task

        self.make_expired()

        with patch(
            "app.skillrun.queue_expiry.mark_queue_expired", return_value=None
        ), patch("app.skillrun.tasks.codex_runner.run_codex_skill") as runner:
            payload = run_skill_task.apply(args=[self.record.id]).get()

        runner.assert_not_called()
        self.assertTrue(payload["skipped"])


if __name__ == "__main__":
    unittest.main()
