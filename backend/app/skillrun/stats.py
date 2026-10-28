"""stats 模块负责技能调用统计的筛选、查询与接口序列化。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import text

from extensions import db


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
SKILL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# 业务结论枚举到中文名称的映射，供接口直接展示。
OUTCOME_LABELS = {
    "passed": "通过",
    "rejected": "退回",
    "needs_fix": "需修改",
    "unknown": "无结论",
    "not_applicable": "不适用",
    "aborted": "中止",
}

# 统计口径：两表按任务与记录标识同时关联，排除不适用记录；
# 耗时优先取统计表，缺失时回退任务表，最后用任务起止时间兜底。
SKILL_STATISTICS_SQL = """
WITH base AS (
    SELECT
        c.id,
        c.skill_id,
        c.issue_key,
        c.jira_url,
        c.execution_status,
        c.outcome,
        c.blocking_count,
        COALESCE(
            NULLIF(c.duration_ms, 0),
            NULLIF(r.duration_ms, 0),
            ROUND(TIMESTAMPDIFF(MICROSECOND, r.started_at, r.finished_at) / 1000, 3)
        ) AS duration_ms,
        COALESCE(r.created_at, c.created_at) AS queue_created_at,
        COALESCE(r.started_at, c.started_at) AS queue_started_at
    FROM skill_call_records AS c
    INNER JOIN skill_run_records AS r
        ON r.id = c.record_id
       AND r.task_id = c.task_id
    WHERE (:start_utc IS NULL OR c.created_at >= :start_utc)
      AND (:end_utc IS NULL OR c.created_at < :end_utc)
      AND (:skill_id IS NULL OR c.skill_id = :skill_id)
      AND c.outcome <> 'not_applicable'
),
p95_ranked AS (
    SELECT
        skill_id,
        duration_ms,
        ROW_NUMBER() OVER (
            PARTITION BY skill_id
            ORDER BY duration_ms
        ) AS rn,
        COUNT(*) OVER (
            PARTITION BY skill_id
        ) AS cnt
    FROM base
    WHERE duration_ms IS NOT NULL
      AND duration_ms > 0
),
p95 AS (
    SELECT
        skill_id,
        CAST(
            ROUND(
                MAX(CASE WHEN rn = CEIL(cnt * 0.95) THEN duration_ms END),
                2
            ) AS DECIMAL(14, 2)
        ) AS p95_duration_ms
    FROM p95_ranked
    GROUP BY skill_id
),
per_skill AS (
    SELECT
        b.skill_id AS skill_id,
        COUNT(*) AS call_count,
        COUNT(
            DISTINCT COALESCE(
                NULLIF(TRIM(b.issue_key), ''),
                NULLIF(TRIM(b.jira_url), '')
            )
        ) AS issue_count,
        SUM(b.execution_status = 'queued') AS queued_count,
        SUM(b.execution_status = 'running') AS running_count,
        SUM(b.execution_status = 'succeeded') AS succeeded_count,
        SUM(b.execution_status = 'failed') AS failed_count,
        SUM(b.execution_status = 'timeout') AS timeout_count,
        SUM(b.execution_status = 'cancelled') AS cancelled_count,
        SUM(b.outcome = 'passed') AS passed_count,
        SUM(b.outcome = 'rejected') AS rejected_count,
        SUM(b.outcome = 'needs_fix') AS needs_fix_count,
        SUM(b.outcome = 'not_applicable') AS not_applicable_count,
        SUM(b.outcome = 'aborted') AS aborted_count,
        SUM(
            b.execution_status IN ('succeeded', 'failed', 'timeout', 'cancelled')
            AND b.outcome = 'unknown'
        ) AS unknown_terminal_count,
        CASE
            WHEN SUM(
                b.execution_status IN ('succeeded', 'failed', 'timeout', 'cancelled')
            ) = 0 THEN NULL
            ELSE ROUND(
                100 * SUM(b.outcome = 'passed') /
                NULLIF(
                    SUM(
                        b.execution_status IN ('succeeded', 'failed', 'timeout', 'cancelled')
                    ),
                    0
                ),
                2
            )
        END AS pass_rate_pct,
        ROUND(
            100 * SUM(b.outcome = 'passed') /
            NULLIF(SUM(b.outcome IN ('passed', 'rejected', 'needs_fix')), 0),
            2
        ) AS explicit_pass_rate_pct,
        CAST(ROUND(AVG(b.duration_ms), 2) AS DECIMAL(14, 2)) AS avg_duration_ms,
        MAX(p.p95_duration_ms) AS p95_duration_ms,
        CAST(
            ROUND(
                AVG(
                    CASE
                        WHEN b.queue_started_at IS NOT NULL THEN
                            GREATEST(
                                TIMESTAMPDIFF(
                                    MICROSECOND,
                                    b.queue_created_at,
                                    b.queue_started_at
                                ),
                                0
                            ) / 1000000
                    END
                ),
                2
            ) AS DECIMAL(14, 2)
        ) AS avg_queue_seconds,
        SUM(COALESCE(b.blocking_count, 0)) AS blocking_total,
        SUM(COALESCE(b.blocking_count, 0) > 0) AS blocking_call_count,
        MAX(b.queue_created_at) AS last_called_at_utc
    FROM base AS b
    LEFT JOIN p95 AS p
        ON p.skill_id = b.skill_id
    GROUP BY b.skill_id
),
total_row AS (
    SELECT
        NULL AS skill_id,
        COUNT(*) AS call_count,
        COUNT(
            DISTINCT COALESCE(
                NULLIF(TRIM(b.issue_key), ''),
                NULLIF(TRIM(b.jira_url), '')
            )
        ) AS issue_count,
        SUM(b.execution_status = 'queued') AS queued_count,
        SUM(b.execution_status = 'running') AS running_count,
        SUM(b.execution_status = 'succeeded') AS succeeded_count,
        SUM(b.execution_status = 'failed') AS failed_count,
        SUM(b.execution_status = 'timeout') AS timeout_count,
        SUM(b.execution_status = 'cancelled') AS cancelled_count,
        SUM(b.outcome = 'passed') AS passed_count,
        SUM(b.outcome = 'rejected') AS rejected_count,
        SUM(b.outcome = 'needs_fix') AS needs_fix_count,
        SUM(b.outcome = 'not_applicable') AS not_applicable_count,
        SUM(b.outcome = 'aborted') AS aborted_count,
        SUM(
            b.execution_status IN ('succeeded', 'failed', 'timeout', 'cancelled')
            AND b.outcome = 'unknown'
        ) AS unknown_terminal_count,
        CASE
            WHEN SUM(
                b.execution_status IN ('succeeded', 'failed', 'timeout', 'cancelled')
            ) = 0 THEN NULL
            ELSE ROUND(
                100 * SUM(b.outcome = 'passed') /
                NULLIF(
                    SUM(
                        b.execution_status IN ('succeeded', 'failed', 'timeout', 'cancelled')
                    ),
                    0
                ),
                2
            )
        END AS pass_rate_pct,
        ROUND(
            100 * SUM(b.outcome = 'passed') /
            NULLIF(SUM(b.outcome IN ('passed', 'rejected', 'needs_fix')), 0),
            2
        ) AS explicit_pass_rate_pct,
        CAST(ROUND(AVG(b.duration_ms), 2) AS DECIMAL(14, 2)) AS avg_duration_ms,
        NULL AS p95_duration_ms,
        CAST(
            ROUND(
                AVG(
                    CASE
                        WHEN b.queue_started_at IS NOT NULL THEN
                            GREATEST(
                                TIMESTAMPDIFF(
                                    MICROSECOND,
                                    b.queue_created_at,
                                    b.queue_started_at
                                ),
                                0
                            ) / 1000000
                    END
                ),
                2
            ) AS DECIMAL(14, 2)
        ) AS avg_queue_seconds,
        SUM(COALESCE(b.blocking_count, 0)) AS blocking_total,
        SUM(COALESCE(b.blocking_count, 0) > 0) AS blocking_call_count,
        MAX(b.queue_created_at) AS last_called_at_utc
    FROM base AS b
),
combined AS (
    SELECT per_skill.*, 0 AS row_type
    FROM per_skill
    UNION ALL
    SELECT total_row.*, 1 AS row_type
    FROM total_row
)
SELECT
    skill_id,
    call_count,
    issue_count,
    queued_count,
    running_count,
    succeeded_count,
    failed_count,
    timeout_count,
    cancelled_count,
    passed_count,
    rejected_count,
    needs_fix_count,
    not_applicable_count,
    aborted_count,
    unknown_terminal_count,
    pass_rate_pct,
    explicit_pass_rate_pct,
    avg_duration_ms,
    p95_duration_ms,
    avg_queue_seconds,
    blocking_total,
    blocking_call_count,
    last_called_at_utc,
    row_type
FROM combined
ORDER BY row_type, call_count DESC, skill_id
"""

ISSUE_STATISTICS_SQL = """
WITH base AS (
    SELECT
        c.id,
        c.skill_id,
        c.issue_key,
        c.jira_url,
        c.execution_status,
        c.outcome,
        c.blocking_count,
        c.summary,
        c.conclusion_text,
        COALESCE(
            NULLIF(c.duration_ms, 0),
            NULLIF(r.duration_ms, 0),
            ROUND(TIMESTAMPDIFF(MICROSECOND, r.started_at, r.finished_at) / 1000, 3)
        ) AS duration_ms,
        COALESCE(r.created_at, c.created_at) AS created_at
    FROM skill_call_records AS c
    INNER JOIN skill_run_records AS r
        ON r.id = c.record_id
       AND r.task_id = c.task_id
    WHERE (:start_utc IS NULL OR c.created_at >= :start_utc)
      AND (:end_utc IS NULL OR c.created_at < :end_utc)
      AND (:skill_id IS NULL OR c.skill_id = :skill_id)
      AND c.outcome <> 'not_applicable'
),
ranked AS (
    SELECT
        b.*,
        ROW_NUMBER() OVER (
            PARTITION BY b.issue_key, b.skill_id
            ORDER BY b.created_at ASC, b.id ASC
        ) AS rn_first,
        ROW_NUMBER() OVER (
            PARTITION BY b.issue_key, b.skill_id
            ORDER BY b.created_at DESC, b.id DESC
        ) AS rn_last
    FROM base AS b
    WHERE b.issue_key IS NOT NULL
      AND TRIM(b.issue_key) <> ''
),
grouped AS (
    SELECT
        issue_key,
        skill_id,
        COUNT(*) AS call_count,
        MAX(CASE WHEN rn_first = 1 THEN outcome END) AS first_outcome,
        MAX(CASE WHEN rn_last = 1 THEN outcome END) AS final_outcome,
        MAX(CASE WHEN rn_last = 1 THEN blocking_count END) AS blocking_count,
        MAX(CASE WHEN rn_last = 1 THEN duration_ms END) AS latest_duration_ms,
        MIN(created_at) AS first_called_at_utc,
        MAX(created_at) AS last_called_at_utc,
        MAX(CASE WHEN rn_last = 1 THEN execution_status END) AS execution_status,
        MAX(CASE WHEN rn_last = 1 THEN conclusion_text END) AS conclusion_text,
        MAX(CASE WHEN rn_last = 1 THEN summary END) AS summary,
        MAX(CASE WHEN rn_last = 1 THEN jira_url END) AS jira_url
    FROM ranked
    GROUP BY issue_key, skill_id
)
SELECT
    issue_key,
    skill_id,
    final_outcome,
    first_outcome,
    call_count,
    blocking_count,
    latest_duration_ms,
    first_called_at_utc,
    last_called_at_utc,
    execution_status,
    conclusion_text,
    summary,
    jira_url
FROM grouped
ORDER BY last_called_at_utc DESC, issue_key, skill_id
"""


class StatsRequestError(ValueError):
    """统计接口筛选参数不合法时抛出的异常。"""

    def __init__(self, message: str, field: str | None = None):
        """保存错误信息与对应参数名，供接口返回。"""
        super().__init__(message)
        self.message = message
        self.field = field


@dataclass(frozen=True)
class StatsFilters:
    """保存转换为 UTC 后的统计筛选条件。"""

    start_utc: datetime | None
    end_utc: datetime | None
    skill_id: str | None

    def to_dict(self) -> dict:
        """返回接口回显用的北京时间筛选条件。"""
        return {
            "start_time": to_shanghai_iso(self.start_utc),
            "end_time": to_shanghai_iso(self.end_utc),
            "skill_id": self.skill_id,
        }


def parse_filters(values) -> StatsFilters:
    """解析 start_time、end_time 与 skill_id，并统一转换为 UTC。"""
    start_utc = parse_time_bound(values.get("start_time"), "start_time")
    end_utc = parse_time_bound(values.get("end_time"), "end_time")
    if start_utc is not None and end_utc is not None and start_utc >= end_utc:
        raise StatsRequestError("start_time 必须早于 end_time", "start_time")
    return StatsFilters(
        start_utc=start_utc,
        end_utc=end_utc,
        skill_id=parse_skill_id(values.get("skill_id")),
    )


def parse_time_bound(raw_value, field: str) -> datetime | None:
    """解析日期或 ISO 时间；无时区按北京时间处理，返回不带时区的 UTC。"""
    raw = str(raw_value or "").strip()
    if not raw:
        return None

    try:
        parsed_date = (
            date.fromisoformat(raw)
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw)
            else None
        )
    except ValueError as exc:
        raise StatsRequestError(f"{field} 时间格式不正确", field) from exc

    if parsed_date is not None:
        moment = datetime.combine(parsed_date, time.min, tzinfo=SHANGHAI_TZ)
    else:
        normalized = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
        try:
            moment = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise StatsRequestError(
                f"{field} 时间格式不正确，请使用 YYYY-MM-DD 或 ISO 8601 时间",
                field,
            ) from exc
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=SHANGHAI_TZ)

    return moment.astimezone(UTC).replace(tzinfo=None)


def parse_skill_id(raw_value) -> str | None:
    """校验可选的 skill_id，空值表示不过滤。"""
    skill_id = str(raw_value or "").strip()
    if not skill_id:
        return None
    if not SKILL_ID_PATTERN.fullmatch(skill_id):
        raise StatsRequestError("skill_id 格式不正确", "skill_id")
    return skill_id


def query_skill_statistics(filters: StatsFilters) -> dict:
    """按技能聚合调用次数、技术状态、业务结论、耗时与阻断项。"""
    rows = (
        db.session.execute(text(SKILL_STATISTICS_SQL), _sql_params(filters))
        .mappings()
        .all()
    )
    items = []
    summary = None
    for row in rows:
        serialized = _serialize_skill_row(row)
        if int(row["row_type"]) == 1:
            serialized["label"] = "合计"
            summary = serialized
        else:
            items.append(serialized)
    return {
        "filters": filters.to_dict(),
        "total": len(items),
        "summary": summary,
        "items": items,
    }


def query_issue_statistics(filters: StatsFilters) -> dict:
    """按需求单与技能聚合首次/最终结论、调用次数与最近一次结果。"""
    rows = (
        db.session.execute(text(ISSUE_STATISTICS_SQL), _sql_params(filters))
        .mappings()
        .all()
    )
    items = [_serialize_issue_row(row) for row in rows]
    return {
        "filters": filters.to_dict(),
        "total": len(items),
        "items": items,
    }


def _sql_params(filters: StatsFilters) -> dict:
    """构造 SQL 绑定参数，空筛选条件以 NULL 传给数据库。"""
    return {
        "start_utc": filters.start_utc,
        "end_utc": filters.end_utc,
        "skill_id": filters.skill_id,
    }


def _serialize_skill_row(row) -> dict:
    """把技能统计行转换为稳定的接口字段。"""
    return {
        "skill_id": row["skill_id"],
        "call_count": _as_int(row["call_count"]),
        "issue_count": _as_int(row["issue_count"]),
        "queued_count": _as_int(row["queued_count"]),
        "running_count": _as_int(row["running_count"]),
        "succeeded_count": _as_int(row["succeeded_count"]),
        "failed_count": _as_int(row["failed_count"]),
        "timeout_count": _as_int(row["timeout_count"]),
        "cancelled_count": _as_int(row["cancelled_count"]),
        "passed_count": _as_int(row["passed_count"]),
        "rejected_count": _as_int(row["rejected_count"]),
        "needs_fix_count": _as_int(row["needs_fix_count"]),
        "not_applicable_count": _as_int(row["not_applicable_count"]),
        "aborted_count": _as_int(row["aborted_count"]),
        "unknown_terminal_count": _as_int(row["unknown_terminal_count"]),
        "pass_rate_pct": _as_decimal_text(row["pass_rate_pct"]),
        "explicit_pass_rate_pct": _as_decimal_text(row["explicit_pass_rate_pct"]),
        "avg_duration_ms": _as_decimal_text(row["avg_duration_ms"]),
        "p95_duration_ms": _as_decimal_text(row["p95_duration_ms"]),
        "avg_queue_seconds": _as_decimal_text(row["avg_queue_seconds"]),
        "blocking_total": _as_int(row["blocking_total"]),
        "blocking_call_count": _as_int(row["blocking_call_count"]),
        "last_called_at": to_shanghai_iso(row["last_called_at_utc"]),
    }


def _serialize_issue_row(row) -> dict:
    """把需求单明细转换为稳定的接口字段。"""
    final_outcome = row["final_outcome"] or "unknown"
    first_outcome = row["first_outcome"] or "unknown"
    return {
        "issue_key": row["issue_key"],
        "skill_id": row["skill_id"],
        "final_outcome": final_outcome,
        "final_outcome_label": OUTCOME_LABELS.get(final_outcome, "无结论"),
        "first_outcome": first_outcome,
        "first_outcome_label": OUTCOME_LABELS.get(first_outcome, "无结论"),
        "call_count": _as_int(row["call_count"]),
        "blocking_count": None if row["blocking_count"] is None else _as_int(row["blocking_count"]),
        "latest_duration_ms": _as_decimal_text(row["latest_duration_ms"]),
        "first_called_at": to_shanghai_iso(row["first_called_at_utc"]),
        "last_called_at": to_shanghai_iso(row["last_called_at_utc"]),
        "execution_status": row["execution_status"],
        "conclusion_text": row["conclusion_text"],
        "summary": row["summary"],
        "jira_url": row["jira_url"],
    }


def _as_int(value) -> int:
    """把 MySQL 的 SUM/COUNT 结果统一转换为整数。"""
    return int(value or 0)


def _as_decimal_text(value) -> str | None:
    """把小数指标格式化为固定两位小数的字符串。"""
    if value is None:
        return None
    return format(Decimal(str(value)), ".2f")


def to_shanghai_iso(value) -> str | None:
    """把数据库中的 UTC 时间转为北京时间 ISO 8601 字符串。"""
    if value is None:
        return None
    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return moment.astimezone(SHANGHAI_TZ).isoformat(timespec="seconds")
