"""routes 模块负责技能执行接口的请求校验、任务提交与结果查询。"""
from __future__ import annotations

import logging
import os
from functools import partial
from urllib.parse import urlparse

from celery.result import AsyncResult
from flask import Blueprint, current_app, g, jsonify, request, send_file

from extensions import celery

from .. import artifacts, env_lock, store, tickets
from ..models.model import SkillRunRecord
from ..registry import SkillError, list_skills, resolve_skill
from ..tasks import run_skill_task


skill_bp = Blueprint("skill", __name__)


class SkillRequestError(Exception):
    """技能接口入参不合法时抛出的异常。"""

    def __init__(self, message: str, missing_fields=None):
        """记录错误信息与缺失字段，供接口直接返回。"""
        super().__init__(message)
        self.message = message
        self.missing_fields = list(missing_fields or [])


def _allowed_hosts() -> set:
    """解析允许访问的 Jira 主机白名单，为空表示不限制。"""
    raw = str(current_app.config.get("SKILL_URL_ALLOWED_HOSTS") or "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _validate_jira_url(value) -> str:
    """校验 jira_url 必填、协议合法且命中主机白名单，返回规范化后的地址。"""
    text = str(value or "").strip()
    if not text:
        raise SkillRequestError("缺少必要字段", ["jira_url"])

    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SkillRequestError("jira_url 必须是合法的 http 或 https 地址", ["jira_url"])

    allowed = _allowed_hosts()
    if allowed and str(parsed.hostname or "").lower() not in allowed:
        raise SkillRequestError(f"jira_url 主机不在白名单内：{parsed.hostname}", ["jira_url"])
    return text


def _validate_inputs(value) -> dict:
    """校验额外输入参数必须是 JSON 对象。"""
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise SkillRequestError("inputs 必须是 JSON 对象", ["inputs"])
    return {str(key): item for key, item in value.items()}


def _current_operator() -> str:
    """返回当前调用者标识，用于记录技能任务的发起人。"""
    username = getattr(getattr(g, "current_user", None), "username", None)
    return str(username or "external-api")


def _error_response(error: SkillRequestError, status_code: int = 400):
    """把入参校验异常转换为统一的错误响应。"""
    payload = {"error": error.message}
    if error.missing_fields:
        payload["missing_fields"] = error.missing_fields
    return jsonify(payload), status_code


@skill_bp.get("/list")
def list_available_skills():
    """列出技能目录中全部可用技能，供外部系统选择 skill_id。"""
    skills = [skill.to_dict() for skill in list_skills(current_app.config.get("SKILLS_DIR"))]
    return jsonify({"skills": skills, "total": len(skills)})


@skill_bp.post("/run")
def submit_skill_run():
    """提交一次技能执行任务，异步调用 Codex Agent 完成技能规定的操作。"""
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return _error_response(SkillRequestError("请求体必须是 JSON 对象"))

    # 服务端配置是技能运行参数的唯一来源：缺配置时直接拒绝，避免技能回落到令牌文件或默认站点。
    missing_env = env_lock.missing_env_keys(current_app.config)
    if missing_env:
        env_lock.log_missing_env(missing_env)
        return (
            jsonify(
                {
                    "error": env_lock.build_missing_env_message(missing_env),
                    "missing_env": missing_env,
                }
            ),
            503,
        )

    skill_id = str(payload.get("skill_id") or "").strip()
    if not skill_id:
        return _error_response(SkillRequestError("缺少必要字段", ["skill_id"]))

    try:
        skill = resolve_skill(current_app.config.get("SKILLS_DIR"), skill_id)
        jira_url = _validate_jira_url(payload.get("jira_url"))
        inputs = _validate_inputs(payload.get("inputs"))
    except SkillError as exc:
        return _error_response(SkillRequestError(str(exc), ["skill_id"]), 404)
    except SkillRequestError as exc:
        return _error_response(exc)

    task_id = store.generate_task_id()
    record = store.create_record(
        skill_id=skill.skill_id,
        jira_url=jira_url,
        inputs=inputs,
        requested_by=_current_operator(),
        task_id=task_id,
    )
    try:
        run_skill_task.apply_async(args=[record.id], task_id=task_id)
    except Exception as exc:  # noqa: BLE001 - 入队失败需要立刻反馈而不是留下假排队记录
        logging.exception("技能任务入队失败：task_id=%s", task_id)
        store.mark_finished(
            record,
            SkillRunRecord.STATUS_FAILED,
            stage="enqueue_failed",
            error_message=f"任务入队失败：{exc}",
        )
        return jsonify({"error": "任务入队失败，请检查队列服务", "task_id": task_id}), 503

    return jsonify(
        {
            "task_id": task_id,
            "record_id": record.id,
            "skill_id": skill.skill_id,
            "status": record.status,
            "status_url": f"/skill/task/{task_id}",
        }
    ), 202


@skill_bp.get("/task/<task_id>")
def get_skill_run(task_id: str):
    """查询技能任务的状态、进度、最终结论与可下载的交付物清单。"""
    record = store.find_record(task_id)
    if record is None:
        return jsonify({"error": "任务不存在", "task_id": task_id}), 404

    payload = record.to_dict()
    # 交付物清单随任务详情一起返回：调用方据此拿到文件名，再去 /artifacts/<文件名> 下载。
    # 下载地址带短时效签名票据，浏览器可以直接打开，不需要在地址栏之外再补请求头。
    # 任务还在跑或工作目录已被清理时这里是空列表，不报错。
    payload["artifacts"] = artifacts.list_artifacts_with_urls(
        record.task_id,
        record.workspace_dir,
        ticket_signer=partial(tickets.sign_download_url, current_app),
    )
    async_result = AsyncResult(task_id, app=celery)
    payload["celery_state"] = async_result.state
    if isinstance(async_result.info, dict) and async_result.state == "PROGRESS":
        payload["live_progress"] = async_result.info
    return jsonify(payload)


@skill_bp.get("/task/<task_id>/artifacts/<path:filename>")
def download_skill_artifact(task_id: str, filename: str):
    """下载技能任务工作目录里的交付物文件（只读，不写工作区）。"""
    record = store.find_record(task_id)
    if record is None:
        return jsonify({"error": "任务不存在", "task_id": task_id}), 404

    try:
        path = artifacts.resolve_artifact(record.workspace_dir, filename)
    except artifacts.ArtifactError as exc:
        # 文件名不合法属入参问题（400），其余按"文件不存在"处理（404）。
        status = 400 if exc.reason == "invalid-name" else 404
        logging.info("技能交付物下载被拒绝：task_id=%s 文件名=%s 原因=%s", task_id, filename, exc.reason)
        return jsonify({"error": exc.message, "reason": exc.reason, "task_id": task_id}), status

    # as_attachment 让浏览器按附件下载；download_name 保留中文原名（Werkzeug 会自动做 RFC 5987 编码）。
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


@skill_bp.get("/tasks")
def list_skill_runs():
    """按创建时间倒序返回最近的技能执行记录。"""
    limit_raw = request.args.get("limit", "20")
    try:
        limit = max(1, min(200, int(limit_raw)))
    except (TypeError, ValueError):
        limit = 20

    records = store.list_records(
        limit=limit,
        skill_id=str(request.args.get("skill_id") or "").strip() or None,
        status=str(request.args.get("status") or "").strip() or None,
    )
    return jsonify({"tasks": [record.to_dict(include_result=False) for record in records], "total": len(records)})


@skill_bp.post("/task/<task_id>/cancel")
def cancel_skill_run(task_id: str):
    """取消排队或执行中的技能任务，并把记录置为已取消。"""
    record = store.find_record(task_id)
    if record is None:
        return jsonify({"error": "任务不存在", "task_id": task_id}), 404

    celery.control.revoke(task_id, terminate=True, signal="SIGTERM")
    if record.status not in SkillRunRecord.TERMINAL_STATUSES:
        store.mark_finished(
            record,
            SkillRunRecord.STATUS_CANCELLED,
            stage="cancelled",
            error_message="任务已请求取消",
        )
    return jsonify({"task_id": task_id, "status": SkillRunRecord.STATUS_CANCELLED}), 202
