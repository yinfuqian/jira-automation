"""技能统计接口测试：覆盖筛选解析、参数校验与查询编排。"""
import sys
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

try:
    from tests.test_skill_routes import create_test_app
except ModuleNotFoundError:  # 兼容以 tests 目录为根直接运行
    from test_skill_routes import create_test_app


class StatsFilterTests(unittest.TestCase):
    def test_date_only_range_uses_beijing_time_and_exclusive_end(self):
        """仅传日期时按北京时间解释，结束日期为开区间。"""
        from app.skillrun import stats

        filters = stats.parse_filters(
            {
                "start_time": "2026-10-26",
                "end_time": "2026-10-29",
                "skill_id": "jira-gate-1",
            }
        )

        self.assertEqual(filters.start_utc, datetime(2026, 10, 25, 16, 0, 0))
        self.assertEqual(filters.end_utc, datetime(2026, 10, 28, 16, 0, 0))
        self.assertEqual(filters.skill_id, "jira-gate-1")
        self.assertEqual(filters.to_dict()["start_time"], "2026-10-26T00:00:00+08:00")
        self.assertEqual(filters.to_dict()["end_time"], "2026-10-29T00:00:00+08:00")

    def test_iso_time_with_offset_is_converted_to_utc(self):
        """带时区的 ISO 时间按原时区换算为 UTC。"""
        from app.skillrun import stats

        filters = stats.parse_filters(
            {
                "start_time": "2026-10-26T08:30:00+08:00",
                "end_time": "2026-10-26T09:00:00Z",
            }
        )

        self.assertEqual(filters.start_utc, datetime(2026, 10, 26, 0, 30, 0))
        self.assertEqual(filters.end_utc, datetime(2026, 10, 26, 9, 0, 0))

    def test_invalid_range_and_skill_id_are_rejected(self):
        """开始时间不早于结束时间、skill_id 非法时抛出参数错误。"""
        from app.skillrun import stats

        with self.assertRaises(stats.StatsRequestError) as range_error:
            stats.parse_filters(
                {"start_time": "2026-10-28", "end_time": "2026-10-28"}
            )
        self.assertEqual(range_error.exception.field, "start_time")

        with self.assertRaises(stats.StatsRequestError) as skill_error:
            stats.parse_filters({"skill_id": "../bad"})
        self.assertEqual(skill_error.exception.field, "skill_id")

    def test_decimal_metrics_keep_two_places(self):
        """耗时、排队时长与通过率统一输出两位小数。"""
        from app.skillrun import stats

        self.assertEqual(stats._as_decimal_text(Decimal("312429")), "312429.00")
        self.assertEqual(stats._as_decimal_text(Decimal("33.7")), "33.70")
        self.assertIsNone(stats._as_decimal_text(None))


class SkillStatsRouteTests(unittest.TestCase):
    def setUp(self):
        """为每个用例准备独立的内存数据库应用。"""
        from extensions import db

        self.app = create_test_app()
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.addCleanup(self._cleanup_context)
        self.client = self.app.test_client()

    def _cleanup_context(self):
        """退出应用上下文并释放测试数据库会话。"""
        from extensions import db

        db.session.remove()
        self.context.pop()

    def test_skill_stats_route_passes_filters_and_returns_payload(self):
        """技能统计接口把解析后的筛选条件传给查询层并原样返回结果。"""
        payload = {
            "filters": {},
            "total": 1,
            "summary": {"label": "合计"},
            "items": [{"skill_id": "jira-gate-1"}],
        }
        with patch(
            "app.skillrun.stats.query_skill_statistics", return_value=payload
        ) as query:
            response = self.client.get(
                "/skill/stats/skills"
                "?start_time=2026-10-26&end_time=2026-10-29&skill_id=jira-gate-1"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["items"][0]["skill_id"], "jira-gate-1")
        filters = query.call_args.args[0]
        self.assertEqual(filters.start_utc, datetime(2026, 10, 25, 16, 0, 0))
        self.assertEqual(filters.end_utc, datetime(2026, 10, 28, 16, 0, 0))
        self.assertEqual(filters.skill_id, "jira-gate-1")

    def test_issue_stats_route_returns_rows(self):
        """需求单统计接口返回查询层结果。"""
        payload = {
            "filters": {},
            "total": 1,
            "items": [{"issue_key": "CALL-1921", "skill_id": "jira-gate-1"}],
        }
        with patch(
            "app.skillrun.stats.query_issue_statistics", return_value=payload
        ):
            response = self.client.get("/skill/stats/issues?skill_id=jira-gate-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["items"][0]["issue_key"], "CALL-1921")

    def test_stats_routes_reject_invalid_filters(self):
        """非法时间或 skill_id 返回 400，不进入数据库查询。"""
        invalid_time = self.client.get("/skill/stats/skills?start_time=not-a-time")
        invalid_skill = self.client.get("/skill/stats/issues?skill_id=../bad")

        self.assertEqual(invalid_time.status_code, 400)
        self.assertEqual(invalid_time.get_json()["field"], "start_time")
        self.assertEqual(invalid_skill.status_code, 400)
        self.assertEqual(invalid_skill.get_json()["field"], "skill_id")
