"""队列过期清理：移除长时间未被消费的技能任务并标记失败。"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from flask import current_app
from redis import Redis

from extensions import db

from . import usage
from .models.model import SkillRunRecord


# 技能任务名必须与 tasks.py 中注册的 Celery 任务名保持一致。
SKILL_TASK_NAME = "skillrun.run_skill"
# 技能任务队列名必须与 CELERY_TASK_ROUTES 中的配置保持一致。
DEFAULT_SKILL_QUEUE = "skill"
# 排队超过 60 分钟仍未被执行即视为过期。
DEFAULT_QUEUE_TTL_SECONDS = 60 * 60
# 单次扫描最多处理的记录数，避免异常情况下长时间占用维护 worker。
MAX_RECORDS_PER_SWEEP = 200
# 过期任务的阶段标识，便于在任务详情与统计表中区分。
QUEUE_EXPIRED_STAGE = "queue_expired"
# 队列消息的三种处理结果：已移除、消息已不存在、当前 broker 不支持精确移除。
REMOVE_RESULT_REMOVED = "removed"
REMOVE_RESULT_MISSING = "missing"
REMOVE_RESULT_UNSUPPORTED = "unsupported"


def build_queue_expired_message(ttl_seconds: int) -> str:
    """生成写入任务记录与错误响应的过期说明。"""
    minutes = max(1, int(ttl_seconds) // 60)
    return (
        f"任务进入队列后等待超过 {minutes} 分钟仍未被执行，已自动移出队列并标记失败；"
        "请检查 skill worker 是否正常运行后重新提交任务。"
    )


def is_queue_expired(record, ttl_seconds: int, now: datetime = None) -> bool:
    """判断排队记录是否已超过允许等待时间。"""
    created_at = getattr(record, "created_at", None)
    if created_at is None:
        return False
    current = now or datetime.utcnow()
    return current - created_at >= timedelta(seconds=max(1, int(ttl_seconds)))


def resolve_skill_queue(config=None) -> str:
    """从 Celery 路由配置中解析技能队列名，缺省使用 skill。"""
    settings = config or current_app.config
    routes = settings.get("CELERY_TASK_ROUTES") or {}
    route = routes.get(SKILL_TASK_NAME) or {}
    if isinstance(route, dict) and route.get("queue"):
        return str(route["queue"])
    return DEFAULT_SKILL_QUEUE


def _decode_task_id(raw) -> str:
    """从 Celery 协议消息中解析任务标识；无法解析时返回空字符串。"""
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        payload = json.loads(text)
    except (TypeError, ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    headers = payload.get("headers")
    if isinstance(headers, dict):
        task_name = str(headers.get("task") or "")
        if task_name and task_name != SKILL_TASK_NAME:
            return ""
        return str(headers.get("id") or "")
    # 兼容旧版协议：任务标识可能直接位于 body 字段。
    body = payload.get("body")
    if isinstance(body, dict):
        return str(body.get("id") or "")
    return ""


def _index_queue_messages(messages) -> dict:
    """按任务标识建立队列消息索引，避免为每条过期记录重复扫描整个队列。"""
    indexed = {}
    for raw in messages:
        task_id = _decode_task_id(raw)
        if task_id and task_id not in indexed:
            indexed[task_id] = raw
    return indexed


def remove_queued_task(redis_client, queue_name: str, task_id: str, messages=None) -> str:
    """按任务标识精确移除 Redis 队列中的消息。

    Redis 队列是 List，Celery 没有提供删除单个待消费消息的公开接口，因此按消息头
    中的 task id 定位原始元素后用 LREM 删除。若消息已被 worker 取走，返回 missing。

    messages 可传入本次清理的队列快照；不传时退化为即时扫描一次队列。
    """
    if redis_client is None:
        return REMOVE_RESULT_UNSUPPORTED
    candidates = messages if messages is not None else redis_client.lrange(queue_name, 0, -1)
    for raw in candidates:
        if _decode_task_id(raw) != str(task_id):
            continue
        removed = int(redis_client.lrem(queue_name, 1, raw) or 0)
        return REMOVE_RESULT_REMOVED if removed > 0 else REMOVE_RESULT_MISSING
    return REMOVE_RESULT_MISSING


def _create_broker_client(config):
    """按 Celery broker 地址创建临时 Redis 客户端；非 Redis broker 返回 None。"""
    broker_url = str(config.get("CELERY_BROKER_URL") or config.get("REDIS_URL") or "")
    if not broker_url.startswith(("redis://", "rediss://")):
        return None
    return Redis.from_url(
        broker_url,
        decode_responses=False,
        socket_keepalive=True,
        socket_connect_timeout=config.get("REDIS_SOCKET_CONNECT_TIMEOUT", 10),
        socket_timeout=config.get("REDIS_SOCKET_TIMEOUT", 60),
        retry_on_timeout=True,
        health_check_interval=config.get("REDIS_HEALTH_CHECK_INTERVAL", 30),
    )


def mark_queue_expired(record, message: str):
    """把仍处于排队状态的记录原子地置为失败，并同步调用统计。"""
    updated = (
        db.session.query(SkillRunRecord)
        .filter(
            SkillRunRecord.id == record.id,
            SkillRunRecord.status == SkillRunRecord.STATUS_QUEUED,
        )
        .update(
            {
                SkillRunRecord.status: SkillRunRecord.STATUS_FAILED,
                SkillRunRecord.stage: QUEUE_EXPIRED_STAGE,
                SkillRunRecord.error_message: message,
                SkillRunRecord.finished_at: datetime.utcnow(),
            },
            synchronize_session=False,
        )
    )
    db.session.commit()
    if not updated:
        db.session.refresh(record)
        return None

    db.session.expire_all()
    fresh_record = db.session.get(SkillRunRecord, record.id)
    usage.finish_call(
        fresh_record,
        execution_status=SkillRunRecord.STATUS_FAILED,
        failure_kind=usage.FAILURE_QUEUE_EXPIRED,
        outcome=usage.SkillCallRecord.OUTCOME_UNKNOWN,
        summary=message,
    )
    return fresh_record


def expire_stale_queued_tasks(
    now: datetime = None,
    ttl_seconds: int = None,
    redis_client=None,
    queue_name: str = None,
) -> dict:
    """扫描过期排队任务，移出 Redis 并标记失败，返回本次处理摘要。"""
    config = current_app.config
    ttl = max(
        1,
        int(
            ttl_seconds
            or config.get("SKILL_QUEUE_TTL_SECONDS")
            or DEFAULT_QUEUE_TTL_SECONDS
        ),
    )
    queue = queue_name or resolve_skill_queue(config)
    current = now or datetime.utcnow()
    cutoff = current - timedelta(seconds=ttl)
    records = (
        SkillRunRecord.query.filter(
            SkillRunRecord.status == SkillRunRecord.STATUS_QUEUED,
            SkillRunRecord.created_at <= cutoff,
        )
        .order_by(SkillRunRecord.created_at.asc())
        .limit(MAX_RECORDS_PER_SWEEP)
        .all()
    )
    summary = {
        "ok": True,
        "ttlSeconds": ttl,
        "queue": queue,
        "scanned": len(records),
        "expired": [],
        "removedFromRedis": 0,
        "missingMessages": 0,
    }
    if not records:
        return summary

    try:
        client = redis_client or _create_broker_client(config)
    except Exception:  # noqa: BLE001 - 配置或网络异常都交由下一轮 Beat 重试
        summary.update({"ok": False, "reason": "redis_unavailable"})
        logging.warning("创建技能队列 Redis 连接失败", exc_info=True)
        return summary
    owns_client = redis_client is None and client is not None
    if client is None:
        summary.update({"ok": False, "reason": "redis_unavailable"})
        logging.warning("技能队列过期清理跳过：broker 不是 Redis 或 Redis 地址未配置")
        return summary

    try:
        # 整批过期记录共用一次队列快照，避免任务积压时产生 O(记录数 × 队列长度) 的扫描。
        messages_by_task = _index_queue_messages(client.lrange(queue, 0, -1))
    except Exception:  # noqa: BLE001 - Redis 暂时不可用时等待下一轮 Beat 重试
        summary.update({"ok": False, "reason": "redis_unavailable"})
        logging.warning("读取技能队列失败，等待下一轮清理重试", exc_info=True)
        return summary

    try:
        for record in records:
            try:
                message = messages_by_task.get(record.task_id)
                removal = remove_queued_task(
                    client,
                    queue,
                    record.task_id,
                    messages=[message] if message is not None else [],
                )
            except Exception:  # noqa: BLE001 - 单个消息删除失败不应中断整批清理
                logging.warning(
                    "移除过期技能任务消息失败：task_id=%s queue=%s",
                    record.task_id,
                    queue,
                    exc_info=True,
                )
                continue
            if removal == REMOVE_RESULT_UNSUPPORTED:
                continue

            # 消息已被 worker 取走时队列本身已不再阻塞；继续按数据库年龄收敛状态。
            try:
                expired_record = mark_queue_expired(record, build_queue_expired_message(ttl))
            except Exception:  # noqa: BLE001 - 单条状态收敛失败不能阻断其余过期任务
                db.session.rollback()
                logging.warning(
                    "标记排队超时任务失败：task_id=%s", record.task_id, exc_info=True
                )
                continue
            if expired_record is None:
                continue
            summary["expired"].append(record.task_id)
            if removal == REMOVE_RESULT_REMOVED:
                summary["removedFromRedis"] += 1
            else:
                summary["missingMessages"] += 1
    finally:
        if owns_client:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - 关闭临时连接失败不影响清理结果
                logging.debug("关闭队列清理 Redis 连接失败", exc_info=True)

    if summary["expired"]:
        logging.warning(
            "已清理 %s 个排队超时的技能任务：%s",
            len(summary["expired"]),
            ", ".join(summary["expired"]),
        )
    return summary
