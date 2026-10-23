"""store 模块负责技能执行记录的持久化读写与状态流转。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from extensions import db

from . import usage
from .models.model import SkillRunRecord


def generate_task_id() -> str:
    """生成全局唯一的技能任务标识。"""
    return f"SKL-{uuid.uuid4().hex[:16].upper()}"


def dump_json(value) -> object:
    """把结构化数据序列化为数据库文本，空值直接落库为空。"""
    if value in (None, "", [], {}):
        return None
    return json.dumps(value, ensure_ascii=False)


def load_json(value, default):
    """把数据库文本安全地还原为结构化数据，解析失败时返回默认值。"""
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def create_record(
    skill_id: str,
    jira_url: str = "",
    inputs: dict = None,
    requested_by: str = "",
    task_id: str = None,
    trigger_source: str = "api",
) -> SkillRunRecord:
    """创建一条排队中的技能执行记录。"""
    record = SkillRunRecord(
        task_id=task_id or generate_task_id(),
        skill_id=skill_id,
        jira_url=jira_url or None,
        inputs=dump_json(inputs or {}),
        requested_by=requested_by or None,
        status=SkillRunRecord.STATUS_QUEUED,
    )
    db.session.add(record)
    db.session.commit()
    usage.ensure_call(record, trigger_source=trigger_source)
    return record


def find_record(task_id: str) -> SkillRunRecord:
    """按任务标识查询技能执行记录。"""
    return SkillRunRecord.query.filter_by(task_id=str(task_id or "")).first()


def list_records(limit: int = 20, skill_id: str = None, status: str = None) -> list:
    """按创建时间倒序返回最近的技能执行记录。"""
    query = SkillRunRecord.query
    if skill_id:
        query = query.filter_by(skill_id=skill_id)
    if status:
        query = query.filter_by(status=status)
    return query.order_by(SkillRunRecord.id.desc()).limit(max(1, int(limit))).all()


def find_active_record(skill_id: str, jira_url: str):
    """查找同一缺陷尚未结束的同技能任务，用于 webhook 重试去重。"""
    return (
        SkillRunRecord.query.filter(
            SkillRunRecord.skill_id == str(skill_id or ""),
            SkillRunRecord.jira_url == str(jira_url or ""),
            SkillRunRecord.status.in_(
                (SkillRunRecord.STATUS_QUEUED, SkillRunRecord.STATUS_RUNNING)
            ),
        )
        .order_by(SkillRunRecord.id.desc())
        .first()
    )


def update_record(record: SkillRunRecord, **fields) -> SkillRunRecord:
    """更新记录字段并提交事务。"""
    for key, value in fields.items():
        setattr(record, key, value)
    db.session.add(record)
    db.session.commit()
    return record


def mark_running(record: SkillRunRecord, workspace_dir: str) -> SkillRunRecord:
    # 把记录置为执行中并登记工作目录。
    record = update_record(
        record,
        status=SkillRunRecord.STATUS_RUNNING,
        stage="starting",
        workspace_dir=workspace_dir,
        started_at=datetime.utcnow(),
        error_message=None,
    )
    usage.mark_running(record)
    return record

def mark_finished(
    record: SkillRunRecord,
    status: str,
    failure_kind: str = None,
    **fields,
) -> SkillRunRecord:
    # 把记录置为终态并同步技能调用统计。
    record = update_record(
        record,
        status=status,
        finished_at=datetime.utcnow(),
        **fields,
    )
    usage.finish_from_result(
        record,
        execution_status=status,
        failure_kind=failure_kind,
    )
    return record
