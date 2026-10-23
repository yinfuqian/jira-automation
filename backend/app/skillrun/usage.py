"""usage 模块负责技能调用统计落库、结果文件解析与业务结论归一化。"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime

from extensions import db

from .models.usage import SkillCallRecord

FAILURE_ENV_MISSING = SkillCallRecord.FAILURE_ENV_MISSING
FAILURE_SKILL_NOT_FOUND = SkillCallRecord.FAILURE_SKILL_NOT_FOUND
FAILURE_ENQUEUE_FAILED = SkillCallRecord.FAILURE_ENQUEUE_FAILED
FAILURE_CODEX_EXCEPTION = SkillCallRecord.FAILURE_CODEX_EXCEPTION
FAILURE_NETWORK = SkillCallRecord.FAILURE_NETWORK
FAILURE_TIMEOUT = SkillCallRecord.FAILURE_TIMEOUT
FAILURE_NON_ZERO_EXIT = SkillCallRecord.FAILURE_NON_ZERO_EXIT
FAILURE_NO_FINAL_MESSAGE = SkillCallRecord.FAILURE_NO_FINAL_MESSAGE
FAILURE_CANCELLED = SkillCallRecord.FAILURE_CANCELLED
FAILURE_WORKER_RESTART = SkillCallRecord.FAILURE_WORKER_RESTART
FAILURE_QUEUE_EXPIRED = SkillCallRecord.FAILURE_QUEUE_EXPIRED


# 顶层技能约定的机器可读结果文件名与最终消息回退标记。
RESULT_FILE_NAME = ".skill-result.json"
RESULT_MARKER = "SKILL_RESULT_JSON"
MAX_RESULT_FILE_BYTES = 64 * 1024
MAX_SUMMARY_LENGTH = 500
MAX_CONCLUSION_LENGTH = 128
ISSUE_KEY_PATTERN = re.compile(r"([A-Za-z][A-Za-z0-9_]*-\d+)")

# 中文业务结论到统一枚举的映射；否定项必须排在肯定项之前。
CONCLUSION_RULES = (
    ("审核未完成", SkillCallRecord.OUTCOME_UNKNOWN),
    ("无法判定", SkillCallRecord.OUTCOME_UNKNOWN),
    ("映射缺失", SkillCallRecord.OUTCOME_UNKNOWN),
    ("未检查", SkillCallRecord.OUTCOME_UNKNOWN),
    ("未完成", SkillCallRecord.OUTCOME_UNKNOWN),
    ("不适用", SkillCallRecord.OUTCOME_NOT_APPLICABLE),
    ("已中止", SkillCallRecord.OUTCOME_ABORTED),
    ("已取消", SkillCallRecord.OUTCOME_ABORTED),
    ("不通过", SkillCallRecord.OUTCOME_REJECTED),
    ("暂不通过", SkillCallRecord.OUTCOME_REJECTED),
    ("退回补充", SkillCallRecord.OUTCOME_REJECTED),
    ("退回补齐", SkillCallRecord.OUTCOME_REJECTED),
    ("不合格", SkillCallRecord.OUTCOME_REJECTED),
    ("缺分析参数", SkillCallRecord.OUTCOME_REJECTED),
    ("打回", SkillCallRecord.OUTCOME_REJECTED),
    ("驳回", SkillCallRecord.OUTCOME_REJECTED),
    ("失败", SkillCallRecord.OUTCOME_REJECTED),
    ("需修改", SkillCallRecord.OUTCOME_NEEDS_FIX),
    ("部分完成", SkillCallRecord.OUTCOME_NEEDS_FIX),
    ("有条件通过", SkillCallRecord.OUTCOME_PASSED),
    ("已自动分析", SkillCallRecord.OUTCOME_PASSED),
    ("放行", SkillCallRecord.OUTCOME_PASSED),
    ("通过", SkillCallRecord.OUTCOME_PASSED),
    ("合格", SkillCallRecord.OUTCOME_PASSED),
    ("已完成", SkillCallRecord.OUTCOME_PASSED),
)


def _now():
    """返回当前 UTC 时间，与原任务表保持一致。"""
    return datetime.utcnow()


def _truncate(value, limit):
    """把文本裁剪到数据库字段长度，空值保持为空。"""
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def extract_issue_key(jira_url):
    """从 Jira 地址中提取单号，提取不到时返回空。"""
    match = ISSUE_KEY_PATTERN.search(str(jira_url or ""))
    return match.group(1) if match else None


def map_conclusion(value):
    """把技能输出的中文结论映射为统计枚举，无法识别时返回空。"""
    text = str(value or "").strip().lower()
    if not text:
        return None
    for keyword, outcome in CONCLUSION_RULES:
        if keyword in text:
            return outcome
    return None


def _find_call(record):
    """按原任务记录标识查找统计行。"""
    record_id = getattr(record, "id", None)
    if record_id is None:
        return None
    return SkillCallRecord.query.filter_by(record_id=record_id).first()


def ensure_call(record, trigger_source="api"):
    """为一次顶层技能任务创建排队统计行；重复调用保持幂等。"""
    if record is None or getattr(record, "id", None) is None:
        return None
    existing = _find_call(record)
    if existing is not None:
        return existing
    try:
        call = SkillCallRecord(
            task_id=record.task_id,
            record_id=record.id,
            skill_id=record.skill_id,
            issue_key=extract_issue_key(record.jira_url),
            jira_url=record.jira_url,
            trigger_source=str(trigger_source or "api"),
            requested_by=record.requested_by,
            execution_status=SkillCallRecord.EXECUTION_QUEUED,
            outcome=SkillCallRecord.OUTCOME_UNKNOWN,
            conclusion_source=SkillCallRecord.SOURCE_NONE,
            created_at=record.created_at or _now(),
            updated_at=_now(),
        )
        db.session.add(call)
        db.session.commit()
        return call
    except Exception:
        db.session.rollback()
        logging.warning("创建技能调用统计失败：task_id=%s", getattr(record, "task_id", ""), exc_info=True)
        return _find_call(record)


def _commit_call(call):
    """提交统计行，失败只记日志，不能影响技能主任务。"""
    try:
        db.session.add(call)
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        logging.warning("写入技能调用统计失败：record_id=%s", getattr(call, "record_id", None), exc_info=True)
        return False


def mark_running(record):
    """把统计行置为执行中并登记开始时间。"""
    call = ensure_call(record)
    if call is None:
        return False
    call.execution_status = SkillCallRecord.EXECUTION_RUNNING
    call.started_at = getattr(record, "started_at", None) or _now()
    call.updated_at = _now()
    return _commit_call(call)


def _default_outcome(execution_status):
    """没有机器结论时的兜底业务结论。"""
    if execution_status == SkillCallRecord.EXECUTION_CANCELLED:
        return SkillCallRecord.OUTCOME_ABORTED
    return SkillCallRecord.OUTCOME_UNKNOWN


def finish_call(
    record,
    execution_status,
    failure_kind=None,
    outcome=None,
    conclusion_text=None,
    conclusion_source=None,
    blocking_count=None,
    summary=None,
    duration_ms=None,
    issue_key=None,
):
    """更新统计行终态；未显式给出业务结论时按技术状态兜底。"""
    call = ensure_call(record)
    if call is None:
        return False
    call.execution_status = execution_status
    call.outcome = outcome or _default_outcome(execution_status)
    if failure_kind is not None:
        call.failure_kind = failure_kind
    if conclusion_text is not None:
        call.conclusion_text = _truncate(conclusion_text, MAX_CONCLUSION_LENGTH)
    if conclusion_source is not None:
        call.conclusion_source = conclusion_source
    if blocking_count is not None:
        call.blocking_count = int(blocking_count)
    if summary is not None:
        call.summary = _truncate(summary, MAX_SUMMARY_LENGTH)
    if issue_key:
        call.issue_key = _truncate(issue_key, 64)
    actual_duration = duration_ms if duration_ms is not None else getattr(record, "duration_ms", None)
    if actual_duration is not None:
        try:
            call.duration_ms = int(actual_duration)
        except (TypeError, ValueError):
            pass
    call.finished_at = getattr(record, "finished_at", None) or _now()
    call.updated_at = _now()
    return _commit_call(call)


def mark_cancelled(record):
    """把取消的任务记为已取消，业务结论记为中止。"""
    return finish_call(
        record,
        SkillCallRecord.EXECUTION_CANCELLED,
        failure_kind=SkillCallRecord.FAILURE_CANCELLED,
        outcome=SkillCallRecord.OUTCOME_ABORTED,
        conclusion_source=SkillCallRecord.SOURCE_NONE,
        summary="任务已请求取消",
    )


def mark_recovered(record):
    """把 worker 重启回收的孤儿任务记为失败。"""
    return finish_call(
        record,
        SkillCallRecord.EXECUTION_FAILED,
        failure_kind=SkillCallRecord.FAILURE_WORKER_RESTART,
        conclusion_source=SkillCallRecord.SOURCE_NONE,
        summary="worker 重启导致任务中断，已自动回收",
    )


def _result_path(workspace_dir):
    """返回任务根目录下的结果文件路径。"""
    if not workspace_dir:
        return None
    return os.path.join(str(workspace_dir), RESULT_FILE_NAME)


def _validate_result_payload(payload, skill_id):
    """校验结果文件契约，返回归一化字段和错误原因。"""
    if not isinstance(payload, dict):
        return None, "payload_not_object"
    if payload.get("schemaVersion") != 1:
        return None, "schema_version_invalid"
    payload_skill = str(payload.get("skillId") or "").strip()
    if not payload_skill or payload_skill != str(skill_id or "").strip():
        return None, "skill_id_mismatch"
    conclusion = str(payload.get("conclusion") or "").strip()
    if not conclusion:
        return None, "conclusion_missing"

    blocking_raw = payload.get("blockingCount")
    blocking_count = None
    if blocking_raw is not None:
        if isinstance(blocking_raw, bool):
            return None, "blocking_count_invalid"
        try:
            blocking_count = int(blocking_raw)
        except (TypeError, ValueError):
            return None, "blocking_count_invalid"
        if blocking_count < 0:
            return None, "blocking_count_invalid"

    return {
        "conclusion": conclusion,
        "outcome": map_conclusion(conclusion),
        "blocking_count": blocking_count,
        "summary": _truncate(payload.get("summary"), MAX_SUMMARY_LENGTH),
        "issue_key": str(payload.get("issueKey") or "").strip() or None,
    }, None


def _read_result_file(path, skill_id):
    """安全读取并校验根目录结果文件，拒绝符号链接和超大文件。"""
    if os.path.islink(path):
        return {"status": "invalid", "reason": "symlink", "from_file": True, "path": path}
    try:
        if not os.path.isfile(path):
            return {"status": "invalid", "reason": "not_regular_file", "from_file": True, "path": path}
        if os.path.getsize(path) > MAX_RESULT_FILE_BYTES:
            return {"status": "invalid", "reason": "file_too_large", "from_file": True, "path": path}
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        logging.warning("读取技能结果文件失败：path=%s", path, exc_info=True)
        return {"status": "invalid", "reason": "read_failed", "from_file": True, "path": path}

    parsed, reason = _validate_result_payload(payload, skill_id)
    if parsed is None:
        return {"status": "invalid", "reason": reason, "from_file": True, "path": path}
    parsed.update({"status": "valid", "from_file": True, "path": path, "source": SkillCallRecord.SOURCE_FILE})
    return parsed


def _read_marker(marker_path, skill_id):
    """从 last-message.md 的最后一条 SKILL_RESULT_JSON 标记读取结论。"""
    if not os.path.isfile(marker_path) or os.path.islink(marker_path):
        return {"status": "none", "from_file": False, "path": None}
    try:
        with open(marker_path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        logging.warning("读取技能最终消息失败：path=%s", marker_path, exc_info=True)
        return {"status": "invalid", "reason": "read_failed", "from_file": False, "path": None}

    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped.startswith(RESULT_MARKER):
            continue
        raw = stripped[len(RESULT_MARKER):].strip()
        if not raw:
            return {"status": "invalid", "reason": "marker_empty", "from_file": False, "path": None}
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return {"status": "invalid", "reason": "marker_invalid_json", "from_file": False, "path": None}
        parsed, reason = _validate_result_payload(payload, skill_id)
        if parsed is None:
            return {"status": "invalid", "reason": reason, "from_file": False, "path": None}
        parsed.update({"status": "valid", "from_file": False, "path": None, "source": SkillCallRecord.SOURCE_MARKER})
        return parsed
    return {"status": "none", "from_file": False, "path": None}


def _read_result(workspace_dir, skill_id):
    """优先读取根目录结果文件，缺失时回退到最终消息标记。"""
    result_path = _result_path(workspace_dir)
    if not result_path:
        return {"status": "none", "from_file": False, "path": None}
    if os.path.lexists(result_path):
        return _read_result_file(result_path, skill_id)
    marker_path = os.path.join(str(workspace_dir), "last-message.md")
    return _read_marker(marker_path, skill_id)


def _remove_result_file(path):
    """统计入库成功后删除结果文件，失败时保留现场。"""
    if not path:
        return
    try:
        if os.path.isfile(path) and not os.path.islink(path):
            os.unlink(path)
    except OSError:
        logging.warning("删除技能结果文件失败：path=%s", path, exc_info=True)


def finish_from_result(record, execution_status=None, failure_kind=None, duration_ms=None, parse_result=True):
    """结束统计行：解析结果文件或标记，并写入业务结论与阻断项数量。"""
    status = execution_status or getattr(record, "status", SkillCallRecord.EXECUTION_FAILED)
    if parse_result:
        parsed = _read_result(getattr(record, "workspace_dir", None), getattr(record, "skill_id", ""))
    else:
        parsed = {"status": "none", "from_file": False, "path": None}

    outcome = _default_outcome(status)
    source = SkillCallRecord.SOURCE_NONE
    conclusion_text = None
    summary = None
    blocking_count = None
    issue_key = None

    if parsed.get("status") == "valid":
        conclusion_text = parsed.get("conclusion")
        summary = parsed.get("summary")
        blocking_count = parsed.get("blocking_count")
        issue_key = parsed.get("issue_key")
        mapped = parsed.get("outcome")
        if mapped is None:
            outcome = SkillCallRecord.OUTCOME_UNKNOWN
            source = SkillCallRecord.SOURCE_INVALID
        else:
            outcome = mapped
            source = parsed.get("source") or SkillCallRecord.SOURCE_FILE
    elif parsed.get("status") == "invalid":
        outcome = SkillCallRecord.OUTCOME_UNKNOWN
        source = SkillCallRecord.SOURCE_INVALID

    ok = finish_call(
        record,
        status,
        failure_kind=failure_kind,
        outcome=outcome,
        conclusion_text=conclusion_text,
        conclusion_source=source,
        blocking_count=blocking_count,
        summary=summary,
        duration_ms=duration_ms,
        issue_key=issue_key,
    )
    if ok and parsed.get("status") == "valid" and parsed.get("from_file"):
        _remove_result_file(parsed.get("path"))
    return ok
