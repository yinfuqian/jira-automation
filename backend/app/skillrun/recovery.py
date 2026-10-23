"""recovery 模块负责在 worker 启动时回收上一次运行遗留的孤儿技能任务。"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from extensions import db

from . import usage
from .models.model import SkillRunRecord


# 与 tasks.py 中注册的 Celery 任务名保持一致，用于从活跃任务里识别技能任务。
SKILL_TASK_NAME = "skillrun.run_skill"
# 查询 Celery 活跃任务的超时时间（秒）。
INSPECT_TIMEOUT_SECONDS = 5.0
# worker 就绪后延迟多久开始回收：刚就绪时控制队列可能尚未应答，过早查询会拿不到活跃任务。
RECOVERY_DELAY_SECONDS = 5.0
# 查询活跃任务的尝试次数与重试间隔（秒）。
RECOVERY_ATTEMPTS = 3
RECOVERY_RETRY_INTERVAL_SECONDS = 5.0
# 孤儿任务回收后写入的阶段标识与说明。
ORPHAN_STAGE = "worker_restarted"
ORPHAN_ERROR_MESSAGE = (
    "worker 重启导致该任务中断，已自动标记为失败；上次执行进度见 progress 字段，"
    "确认后可重新提交任务。"
)


def collect_active_record_ids(celery_app, timeout: float = INSPECT_TIMEOUT_SECONDS):
    """收集仍被 worker 执行的技能任务记录标识；没有任何 worker 应答时返回 None。"""
    try:
        inspector = celery_app.control.inspect(timeout=timeout)
        active = inspector.active() if inspector is not None else None
    except Exception:  # noqa: BLE001 - 无法查询时保持原状，不影响 worker 启动
        logging.warning("查询 Celery 活跃任务失败，本次跳过孤儿任务回收", exc_info=True)
        return None
    if not active:
        # 没有任何 worker 应答时无法判断任务归属，交由调用方保持原状而不是贸然回收。
        return None

    record_ids = set()
    for tasks in active.values():
        for task in tasks or ():
            if not isinstance(task, dict) or task.get("name") != SKILL_TASK_NAME:
                continue
            # 技能任务只接收一个参数 record_id，用它反查本地记录。
            for arg in task.get("args") or ():
                try:
                    record_ids.add(int(arg))
                except (TypeError, ValueError):
                    continue
                break
    return record_ids


def recover_orphan_tasks(
    celery_app=None,
    active_record_ids=None,
    timeout: float = INSPECT_TIMEOUT_SECONDS,
    started_before: datetime = None,
) -> list:
    """把执行中但已无对应活跃任务的记录标记为失败，返回被回收的任务标识列表。

    `started_before` 用于限定只回收该时刻之前启动的任务，避免误伤回收期间刚被取走的新任务。
    """
    records = SkillRunRecord.query.filter_by(status=SkillRunRecord.STATUS_RUNNING).all()
    if not records:
        return []

    if active_record_ids is None:
        if celery_app is None:
            # 没有可查询的 Celery 实例时无法确认任务归属，保持原状而不是贸然回收。
            logging.warning("未提供 Celery 实例，跳过孤儿任务回收")
            return []
        active_record_ids = collect_active_record_ids(celery_app, timeout=timeout)
    if active_record_ids is None:
        # 无法确认哪些任务仍然存活时保持原状，避免误杀其它 worker 正在执行的任务。
        logging.warning("无法确认活跃技能任务，跳过孤儿任务回收")
        return []

    recovered = []
    finished_at = datetime.utcnow()
    for record in records:
        if record.id in active_record_ids:
            continue
        if started_before is not None and record.started_at is not None and record.started_at >= started_before:
            # 该任务是在本次查询开始之后才启动的，说明有 worker 正在正常执行它。
            continue
        record.status = SkillRunRecord.STATUS_FAILED
        record.stage = ORPHAN_STAGE
        record.error_message = ORPHAN_ERROR_MESSAGE
        record.finished_at = finished_at
        recovered.append(record.task_id)

    if recovered:
        db.session.commit()
        for record in records:
            if record.task_id in recovered:
                usage.mark_recovered(record)
        logging.warning("已回收 %s 个 worker 重启遗留的技能任务：%s", len(recovered), ", ".join(recovered))
    return recovered


def recover_orphan_tasks_with_retry(
    app,
    celery_app,
    delay_seconds: float = RECOVERY_DELAY_SECONDS,
    attempts: int = RECOVERY_ATTEMPTS,
    interval_seconds: float = RECOVERY_RETRY_INTERVAL_SECONDS,
    sleep=time.sleep,
) -> list:
    """等待 worker 完全就绪后回收孤儿任务；始终无法确认活跃任务时保持原状并提示人工确认。"""
    if delay_seconds > 0:
        sleep(delay_seconds)

    pending = []
    total_attempts = max(1, int(attempts))
    for attempt in range(1, total_attempts + 1):
        # 先取当前时刻，任何在这之后启动的任务都不属于本次回收范围。
        started_before = datetime.utcnow()
        with app.app_context():
            active_record_ids = collect_active_record_ids(celery_app)
            if active_record_ids is not None:
                return recover_orphan_tasks(active_record_ids=active_record_ids, started_before=started_before)
            pending = [
                record.task_id
                for record in SkillRunRecord.query.filter_by(status=SkillRunRecord.STATUS_RUNNING).all()
                if record.started_at is None or record.started_at < started_before
            ]
        if attempt < total_attempts:
            sleep(interval_seconds)

    if pending:
        logging.warning(
            "无法确认活跃技能任务，以下任务仍保持执行中状态，请人工确认：%s", ", ".join(pending)
        )
    return []


def install_worker_recovery(app, celery_app) -> bool:
    """在 worker 就绪时回收孤儿技能任务，返回本次是否完成注册。"""
    if getattr(celery_app, "_skill_orphan_recovery_installed", False):
        return False

    from celery.signals import worker_ready

    def _recover_in_background():
        """在后台线程中执行回收，避免阻塞 worker 启动。"""
        try:
            recover_orphan_tasks_with_retry(app, celery_app)
        except Exception:  # noqa: BLE001 - 回收失败不能阻止 worker 提供服务
            logging.warning("worker 启动时的孤儿任务回收失败", exc_info=True)

    def on_worker_ready(**_kwargs):
        """worker 就绪后在后台线程中回收上一次运行遗留的孤儿任务。"""
        threading.Thread(target=_recover_in_background, name="skill-orphan-recovery", daemon=True).start()

    worker_ready.connect(on_worker_ready, weak=False)
    celery_app._skill_orphan_recovery_installed = True
    return True
