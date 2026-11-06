"""tasks 模块定义机器人问答的 Celery 任务。

网关只负责「收消息 / 发消息」，耗时分析与代码检出都在这里执行，
结果通过 Redis 发布回网关，由网关负责按协议回推流式消息。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime

from flask import current_app

import extensions
from extensions import celery, db

from . import answer, repos
from .models.model import ChatbotQuestion
from .settings import load_settings


logger = logging.getLogger(__name__)

# MySQL TEXT 列上限约 64 KB，留出余量按字节截断，避免长答案导致回写失败。
_DB_TEXT_LIMIT_BYTES = 60000


def _truncate_utf8(value, limit=_DB_TEXT_LIMIT_BYTES):
    """按 UTF-8 字节上限截断文本，保证不会切断多字节字符。"""
    text = str(value or "")
    if len(text.encode("utf-8")) <= limit:
        return text
    parts = []
    size = 0
    for char in text:
        char_size = len(char.encode("utf-8"))
        if size + char_size > limit:
            break
        parts.append(char)
        size += char_size
    return "".join(parts) + "\n\n（内容过长，已截断存储）"


def _split_extra_args(value):
    """把配置中的附加命令行参数拆分为参数列表。"""
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item or "").strip())
    return tuple(item for item in str(value or "").split() if item)


def _workspace_root(config, task_id):
    """返回本次问答的独立工作目录。"""
    root = str(config.get("SKILL_WORKSPACE_DIR") or os.path.join(os.sep, "tmp", "skill-workspace"))
    return os.path.join(root, "chatbot", task_id)


def _codex_options(config, settings, task_id):
    """从 Flask 配置提取 Codex 调用参数，并给每个任务独立的 CODEX_HOME。"""
    codex_home_root = str(config.get("CODEX_HOME") or "").strip()
    task_home = os.path.join(codex_home_root, "runs", f"chatbot-{task_id}") if codex_home_root else ""
    return answer.CodexOptions(
        codex_bin=str(config.get("CODEX_BIN") or "codex"),
        model=str(config.get("CODEX_MODEL") or ""),
        model_provider=str(config.get("CODEX_MODEL_PROVIDER") or ""),
        base_url=str(config.get("CODEX_BASE_URL") or ""),
        wire_api=str(config.get("CODEX_WIRE_API") or "responses"),
        api_key=str(config.get("CODEX_API_KEY") or ""),
        api_key_env=str(config.get("CODEX_API_KEY_ENV") or "CODEX_SKILL_API_KEY"),
        codex_home=task_home,
        sandbox=settings.answer.sandbox,
        ephemeral=bool(config.get("CODEX_EPHEMERAL", True)),
        reasoning_effort=settings.answer.reasoning_effort,
        timeout_seconds=settings.answer.timeout_seconds,
        extra_args=_split_extra_args(config.get("CODEX_EXTRA_ARGS")),
    )


def _upsert_record(payload, status="running"):
    """创建问答记录；同一 task_id 重复投递时复用已有行。"""
    task_id = str(payload.get("task_id") or "").strip()
    record = ChatbotQuestion.query.filter_by(task_id=task_id).first()
    if record is None:
        record = ChatbotQuestion(
            task_id=task_id,
            chatid=str(payload.get("chatid") or ""),
            chattype=str(payload.get("chattype") or ""),
            userid=str(payload.get("userid") or ""),
            project_key=str(payload.get("project_key") or ""),
            project_name=str(payload.get("project_name") or ""),
            question=str(payload.get("question") or ""),
            status=status,
        )
        db.session.add(record)
    else:
        record.status = status
    db.session.commit()
    return record


def _finish_record(record, status, result):
    """回写问答结果与耗时。"""
    record.status = status
    record.answer = _truncate_utf8(result.get("answer")) or None
    record.error = _truncate_utf8(result.get("error")) or None
    record.duration_ms = int(result.get("duration_ms") or 0)
    record.finished_at = datetime.utcnow()
    db.session.commit()


def publish_result(channel, payload):
    """把结果发布到 Redis 频道；网关据此回推流式消息。"""
    client = extensions.redis
    if client is None:
        raise RuntimeError("Redis 未初始化，无法回推问答结果")
    client.publish(channel, json.dumps(payload, ensure_ascii=False))


@celery.task(bind=True, name="chatbot.answer")
def answer_task(self, payload):
    """执行一次代码问答：检出代码 → Codex 分析 → 回写记录 → 通知网关。"""
    payload = payload or {}
    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        logger.error("机器人问答任务缺少 task_id，已丢弃：%s", payload)
        return {"ok": False, "error": "missing task_id"}

    config = current_app.config
    try:
        settings = load_settings()
        channel = settings.gateway.reply_channel
    except Exception as exc:  # noqa: BLE001 - 配置读取的任何异常都要回推给用户
        # 配置异常无法分析，但仍要告诉用户原因，不能让流式消息一直悬着。
        logger.exception("机器人配置不可用：task_id=%s", task_id)
        reply = {
            "task_id": task_id,
            "req_id": payload.get("req_id") or "",
            "stream_id": payload.get("stream_id") or "",
            "chatid": payload.get("chatid") or "",
            "chattype": payload.get("chattype") or "",
            "started_at": payload.get("started_at") or 0,
            "ok": False,
            "answer": "",
            "error": f"机器人配置不可用：{exc}",
            "duration_ms": 0,
            "usage": {},
        }
        try:
            publish_result("chatbot:reply", reply)
        except Exception:  # noqa: BLE001 - 回推失败时用户侧由网关超时兜底
            logger.exception("机器人配置错误结果回推失败：task_id=%s", task_id)
        return {"task_id": task_id, "ok": False, "error": reply["error"]}

    record = None
    try:
        record = _upsert_record(payload)
    except Exception:  # noqa: BLE001 - 审计写不进去时仍然要回答用户
        logger.exception("机器人问答记录创建失败：task_id=%s", task_id)
        db.session.rollback()

    project = settings.project_by_key(payload.get("project_key"))
    result = {"ok": False, "answer": "", "error": "", "duration_ms": 0, "usage": {}}
    workspace = None
    try:
        if project is None:
            result["error"] = "项目已不存在，请重新选择项目后再提问"
        elif not project.module_items():
            result["error"] = f"项目「{project.name}」没有配置任何模块分支"
        else:
            workspace_root = _workspace_root(config, task_id)
            lock_client = extensions.redis
            workspace = answer.prepare_workspace(project, workspace_root, settings, lock_client)
            result = answer.run_answer(
                payload.get("question"),
                project,
                workspace,
                _codex_options(config, settings, task_id),
                max_answer_chars=settings.answer.max_answer_chars,
            )
    except Exception as exc:  # noqa: BLE001 - 任务失败也要回推给用户
        logger.exception("机器人问答执行异常：task_id=%s", task_id)
        result = {
            "ok": False,
            "answer": "",
            "error": f"分析过程异常：{exc}",
            "duration_ms": result.get("duration_ms") or 0,
            "usage": {},
        }
    finally:
        if workspace is not None:
            workspace.cleanup(settings)

    if record is not None:
        try:
            _finish_record(record, "succeeded" if result.get("ok") else "failed", result)
        except Exception:  # noqa: BLE001 - 审计写入失败不能阻断用户拿到答案
            logger.exception("机器人问答记录回写失败：task_id=%s", task_id)
            db.session.rollback()

    reply = {
        "task_id": task_id,
        "req_id": payload.get("req_id") or "",
        "stream_id": payload.get("stream_id") or "",
        "chatid": payload.get("chatid") or "",
        "chattype": payload.get("chattype") or "",
        "userid": payload.get("userid") or "",
        "project_key": payload.get("project_key") or "",
        "project_name": payload.get("project_name") or "",
        "started_at": payload.get("started_at") or 0,
        "ok": bool(result.get("ok")),
        "answer": result.get("answer") or "",
        "error": result.get("error") or "",
        "duration_ms": int(result.get("duration_ms") or 0),
        "usage": result.get("usage") or {},
    }
    try:
        publish_result(channel, reply)
    except Exception:  # noqa: BLE001 - 回推失败时用户侧由网关超时兜底
        logger.exception("机器人问答结果回推失败：task_id=%s", task_id)

    # 工作树已清理，这里再删任务级目录，避免磁盘无界增长。
    try:
        shutil.rmtree(_workspace_root(config, task_id), ignore_errors=True)
    except OSError:
        logger.debug("清理问答工作目录失败：%s", task_id, exc_info=True)

    return {"task_id": task_id, "ok": reply["ok"], "error": reply["error"]}
