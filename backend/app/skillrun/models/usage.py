"""usage 模块定义技能调用统计表，用于按 skill 汇总执行次数与业务结论。"""
from datetime import datetime

from extensions import db


class SkillCallRecord(db.Model):
    """SkillCallRecord 记录平台发起的一次顶层技能任务，不记录技能内部子任务。"""

    __tablename__ = "skill_call_records"

    # 技术执行状态：排队、执行中、成功、失败、超时、已取消。
    EXECUTION_QUEUED = "queued"
    EXECUTION_RUNNING = "running"
    EXECUTION_SUCCEEDED = "succeeded"
    EXECUTION_FAILED = "failed"
    EXECUTION_TIMEOUT = "timeout"
    EXECUTION_CANCELLED = "cancelled"
    TERMINAL_EXECUTION_STATUSES = (
        EXECUTION_SUCCEEDED,
        EXECUTION_FAILED,
        EXECUTION_TIMEOUT,
        EXECUTION_CANCELLED,
    )

    # 业务结论：通过、退回、需修改、未知、不适用、中止。
    OUTCOME_PASSED = "passed"
    OUTCOME_REJECTED = "rejected"
    OUTCOME_NEEDS_FIX = "needs_fix"
    OUTCOME_UNKNOWN = "unknown"
    OUTCOME_NOT_APPLICABLE = "not_applicable"
    OUTCOME_ABORTED = "aborted"

    # 结论来源：结果文件、最终消息标记、无结论、非法结论。
    SOURCE_FILE = "file"
    SOURCE_MARKER = "marker"
    SOURCE_NONE = "none"
    SOURCE_INVALID = "invalid"

    # 失败归类用于区分平台问题与技能业务结论。
    FAILURE_ENV_MISSING = "env_missing"
    FAILURE_SKILL_NOT_FOUND = "skill_not_found"
    FAILURE_ENQUEUE_FAILED = "enqueue_failed"
    FAILURE_CODEX_EXCEPTION = "codex_exception"
    FAILURE_NETWORK = "network"
    FAILURE_TIMEOUT = "timeout"
    FAILURE_NON_ZERO_EXIT = "non_zero_exit"
    FAILURE_NO_FINAL_MESSAGE = "no_final_message"
    FAILURE_CANCELLED = "cancelled"
    FAILURE_WORKER_RESTART = "worker_restart"
    FAILURE_QUEUE_EXPIRED = "queue_expired"

    id = db.Column(
        db.BigInteger().with_variant(db.Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    # 逻辑关联 skill_run_records，不建立物理外键，避免历史库或清理任务互相阻塞。
    task_id = db.Column(db.String(64), nullable=False)
    record_id = db.Column(db.Integer, nullable=True)
    skill_id = db.Column(db.String(64), nullable=False)
    skill_version = db.Column(db.String(64), nullable=True)
    issue_key = db.Column(db.String(64), nullable=True)
    jira_url = db.Column(db.String(500), nullable=True)
    trigger_source = db.Column(db.String(32), nullable=False, default="api")
    requested_by = db.Column(db.String(255), nullable=True)
    execution_status = db.Column(
        db.String(32), nullable=False, default=EXECUTION_QUEUED
    )
    outcome = db.Column(db.String(16), nullable=False, default=OUTCOME_UNKNOWN)
    conclusion_text = db.Column(db.String(128), nullable=True)
    conclusion_source = db.Column(db.String(16), nullable=False, default=SOURCE_NONE)
    failure_kind = db.Column(db.String(32), nullable=True)
    blocking_count = db.Column(db.Integer, nullable=True)
    summary = db.Column(db.String(500), nullable=True)
    duration_ms = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        db.UniqueConstraint("task_id", name="uq_skill_call_task"),
        db.UniqueConstraint("record_id", name="uq_skill_call_record"),
        db.Index("ix_skill_call_skill_created", "skill_id", "created_at"),
        db.Index("ix_skill_call_status_created", "execution_status", "created_at"),
        db.Index("ix_skill_call_outcome_created", "outcome", "created_at"),
        db.Index("ix_skill_call_issue_created", "issue_key", "created_at"),
        db.Index("ix_skill_call_source_created", "conclusion_source", "created_at"),
    )

    def to_dict(self) -> dict:
        """转换为便于测试与人工排查的字典结构。"""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "record_id": self.record_id,
            "skill_id": self.skill_id,
            "skill_version": self.skill_version,
            "issue_key": self.issue_key,
            "jira_url": self.jira_url,
            "trigger_source": self.trigger_source,
            "requested_by": self.requested_by,
            "execution_status": self.execution_status,
            "outcome": self.outcome,
            "conclusion_text": self.conclusion_text,
            "conclusion_source": self.conclusion_source,
            "failure_kind": self.failure_kind,
            "blocking_count": self.blocking_count,
            "summary": self.summary,
            "duration_ms": self.duration_ms,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
