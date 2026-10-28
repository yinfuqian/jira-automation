"""routes 模块负责技能执行接口的请求校验、任务提交与结果查询。"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
from functools import partial
from urllib.parse import urlparse

from celery.result import AsyncResult
from flask import Blueprint, current_app, g, jsonify, request, send_file
from sqlalchemy.exc import SQLAlchemyError

from extensions import celery

from .. import artifacts, env_lock, queue_expiry, stats, store, tickets, usage
from ..models.model import SkillRunRecord
from ..registry import SkillError, list_skills, resolve_skill
from ..tasks import run_skill_task


skill_bp = Blueprint("skill", __name__)

JIRA_ISSUE_KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-\d+$")


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
        configured = str(current_app.config.get("JIRA_BASE_URL") or "").strip() or "(未配置)"
        raise SkillRequestError(
            "jira_url 主机不在白名单内："
            f"{parsed.hostname}；实际允许：{', '.join(sorted(allowed))}；"
            f"JIRA_BASE_URL={configured}。修改 .env 后请用 restart-dev.sh 或 restart-master.sh 重建容器。",
            ["jira_url"],
        )
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


def _create_and_enqueue_skill(skill, jira_url: str, inputs: dict, requested_by: str, trigger_source: str = "api"):
    """创建任务记录并投递 Celery；返回记录和入队错误（成功时为空）。"""
    task_id = store.generate_task_id()
    record = store.create_record(
        skill_id=skill.skill_id,
        jira_url=jira_url,
        inputs=inputs,
        requested_by=requested_by,
        task_id=task_id,
        trigger_source=trigger_source,
    )
    try:
        queue_ttl = int(
            current_app.config.get("SKILL_QUEUE_TTL_SECONDS")
            or queue_expiry.DEFAULT_QUEUE_TTL_SECONDS
        )
        # 消息在队列中超过 TTL 后即使被取出也不再执行；数据库记录由周期清理任务同步置为失败。
        run_skill_task.apply_async(
            args=[record.id],
            task_id=task_id,
            expires=queue_ttl,
            queue="skill",
        )
    except Exception as exc:  # noqa: BLE001 - 入队失败需要立刻反馈而不是留下假排队记录
        logging.exception("技能任务入队失败：task_id=%s", task_id)
        store.mark_finished(
            record,
            SkillRunRecord.STATUS_FAILED,
            stage="enqueue_failed",
            failure_kind=usage.FAILURE_ENQUEUE_FAILED,
            error_message=f"任务入队失败：{exc}",
        )
        return record, str(exc)
    return record, None


def _webhook_api_token_matches() -> bool:
    """校验 Jira 自动化规则携带的 X-API-Token 或 Bearer 令牌。"""
    provided = str(request.headers.get("X-API-Token") or "").strip()
    if not provided:
        scheme, separator, token = str(request.headers.get("Authorization") or "").partition(" ")
        if separator and scheme.lower() == "bearer":
            provided = token.strip()
    configured = str(current_app.config.get("SKILL_API_TOKEN") or "").strip()
    return bool(provided and configured and secrets.compare_digest(provided, configured))


def _webhook_signature_matches() -> bool:
    """校验 Jira 原生 webhook 的 HMAC-SHA256 签名。"""
    secret = str(current_app.config.get("JIRA_WEBHOOK_SECRET") or "").strip()
    if not secret:
        return False
    signature = str(
        request.headers.get("X-Hub-Signature-256")
        or request.headers.get("X-Hub-Signature")
        or ""
    ).strip()
    if not signature:
        return False
    digest = hmac.new(secret.encode("utf-8"), request.get_data(cache=True), hashlib.sha256).hexdigest()
    return secrets.compare_digest(signature, digest) or secrets.compare_digest(
        signature, f"sha256={digest}"
    )


def _jira_webhook_authorized() -> bool:
    """Jira webhook 允许使用接口令牌或原生 HMAC 签名中的任意一种。"""
    return _webhook_api_token_matches() or _webhook_signature_matches()


def _jira_webhook_event(payload: dict) -> str:
    """从 Jira 原生 webhook 或 Jira Automation 自定义请求体中读取事件名。"""
    return str(
        payload.get("webhookEvent")
        or payload.get("issue_event_type_name")
        or payload.get("event")
        or ""
    ).strip()


def _jira_issue_key(payload: dict) -> str:
    """从 Jira webhook 请求体中提取单号，拒绝不符合 Jira 单号格式的值。"""
    issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
    candidate = (
        issue.get("key")
        or payload.get("issueKey")
        or payload.get("issue_key")
        or payload.get("key")
        or ""
    )
    if not candidate and payload.get("jira_url"):
        candidate = urlparse(str(payload.get("jira_url"))).path.rstrip("/").rsplit("/", 1)[-1]
    key = str(candidate or "").strip()
    return key if JIRA_ISSUE_KEY_PATTERN.match(key) else ""


def _jira_issue_type(payload: dict) -> str:
    """读取 Jira 问题类型名称，供自动触发前的缺陷类型过滤使用。"""
    issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    issue_type = fields.get("issuetype") if isinstance(fields.get("issuetype"), dict) else {}
    return str(issue_type.get("name") or "").strip()


@skill_bp.get("/list")
def list_available_skills():
    """列出技能目录中全部可用技能，供外部系统选择 skill_id。"""
    skills = [skill.to_dict() for skill in list_skills(current_app.config.get("SKILLS_DIR"))]
    return jsonify({"skills": skills, "total": len(skills)})


@skill_bp.get("/stats/skills")
def get_skill_statistics():
    """按技能查询调用次数、执行状态、业务结论、耗时与阻断项统计。"""
    try:
        filters = stats.parse_filters(request.args)
    except stats.StatsRequestError as exc:
        return jsonify({"error": exc.message, "field": exc.field}), 400

    try:
        return jsonify(stats.query_skill_statistics(filters))
    except SQLAlchemyError:
        logging.exception("技能统计查询失败：filters=%s", filters)
        return jsonify({"error": "技能统计查询失败"}), 500


@skill_bp.get("/stats/issues")
def get_issue_statistics():
    """按需求单与技能查询首次结论、最终结论及最近一次执行明细。"""
    try:
        filters = stats.parse_filters(request.args)
    except stats.StatsRequestError as exc:
        return jsonify({"error": exc.message, "field": exc.field}), 400

    try:
        return jsonify(stats.query_issue_statistics(filters))
    except SQLAlchemyError:
        logging.exception("需求单统计查询失败：filters=%s", filters)
        return jsonify({"error": "需求单统计查询失败"}), 500


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

    record, enqueue_error = _create_and_enqueue_skill(
        skill, jira_url, inputs, _current_operator()
    )
    if enqueue_error:
        return jsonify({"error": "任务入队失败，请检查队列服务", "task_id": record.task_id}), 503

    return jsonify(
        {
            "task_id": record.task_id,
            "record_id": record.id,
            "skill_id": skill.skill_id,
            "status": record.status,
            "status_url": f"/skill/task/{record.task_id}",
        }
    ), 202


@skill_bp.post("/jira/events")
def jira_webhook():
    """接收 Jira 新建缺陷事件并自动提交 jira-gate-bug，供 Jira 规则或原生 webhook 调用。"""
    if not _jira_webhook_authorized():
        return jsonify({"error": "Jira webhook 鉴权失败"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error_response(SkillRequestError("Jira webhook 请求体必须是 JSON 对象"))

    event = _jira_webhook_event(payload)
    allowed_events = {
        item.strip().lower()
        for item in str(current_app.config.get("JIRA_WEBHOOK_EVENTS") or "").split(",")
        if item.strip()
    }
    if not event:
        return _error_response(
            SkillRequestError("Jira webhook 缺少事件名", ["webhookEvent"])
        )
    if allowed_events and event.lower() not in allowed_events:
        return jsonify({"status": "ignored", "reason": "event_not_allowed", "event": event}), 202

    issue_type = _jira_issue_type(payload)
    allowed_types = {
        item.strip().lower()
        for item in str(current_app.config.get("JIRA_WEBHOOK_ISSUE_TYPES") or "").split(",")
        if item.strip()
    }
    normalized_issue_type = issue_type.lower()
    if allowed_types and issue_type and not any(
        item in normalized_issue_type for item in allowed_types
    ):
        return (
            jsonify(
                {
                    "status": "ignored",
                    "reason": "issue_type_not_allowed",
                    "issue_type": issue_type,
                }
            ),
            202,
        )

    issue_key = _jira_issue_key(payload)
    if not issue_key:
        return _error_response(
            SkillRequestError("Jira webhook 缺少合法的 issue.key", ["issue.key"])
        )

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

    skill_id = str(current_app.config.get("JIRA_WEBHOOK_SKILL_ID") or "jira-gate-bug")
    try:
        skill = resolve_skill(current_app.config.get("SKILLS_DIR"), skill_id)
        jira_base = str(current_app.config.get("JIRA_BASE_URL") or "").rstrip("/")
        jira_url = _validate_jira_url(f"{jira_base}/browse/{issue_key}")
    except SkillError as exc:
        return _error_response(SkillRequestError(str(exc), ["skill_id"]), 404)
    except SkillRequestError as exc:
        return _error_response(exc)

    existing = store.find_active_record(skill.skill_id, jira_url)
    if existing is not None:
        return (
            jsonify(
                {
                    "task_id": existing.task_id,
                    "record_id": existing.id,
                    "skill_id": existing.skill_id,
                    "status": existing.status,
                    "deduplicated": True,
                }
            ),
            202,
        )

    record, enqueue_error = _create_and_enqueue_skill(
        skill, jira_url, {}, "jira-webhook", trigger_source="webhook"
    )
    if enqueue_error:
        return jsonify({"error": "任务入队失败，请检查队列服务", "task_id": record.task_id}), 503

    return (
        jsonify(
            {
                "task_id": record.task_id,
                "record_id": record.id,
                "skill_id": skill.skill_id,
                "status": record.status,
                "status_url": f"/skill/task/{record.task_id}",
                "issue_key": issue_key,
                "deduplicated": False,
            }
        ),
        202,
    )


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
            failure_kind=usage.FAILURE_CANCELLED,
            error_message="任务已请求取消",
        )
    return jsonify({"task_id": task_id, "status": SkillRunRecord.STATUS_CANCELLED}), 202
