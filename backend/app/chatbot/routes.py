"""routes 模块暴露机器人配置与问答记录的只读接口，便于运维核对。"""
from __future__ import annotations

import time

from flask import Blueprint, jsonify, request

import extensions
from . import knowledge, session as session_module
from .models.model import ChatbotQuestion
from .settings import ChatbotConfigError, load_settings
# 显式导入任务模块：Celery worker 通过 create_app() 装配蓝图时才会注册 chatbot.answer。
from . import tasks  # noqa: F401


chatbot_bp = Blueprint("chatbot", __name__)


def _settings():
    """按服务端配置加载机器人设置。"""
    return load_settings()


def _alive_at():
    """读取网关心跳时间戳，Redis 不可用时返回 0。"""
    client = getattr(extensions, "redis", None)
    if client is None:
        return 0.0
    try:
        return session_module.ChatSessionStore(client).alive_at()
    except Exception:  # noqa: BLE001 - 健康检查不应因 Redis 抖动报错
        return 0.0


@chatbot_bp.route("/health", methods=["GET"])
def health():
    """返回机器人配置与长连接网关的存活状态。"""
    try:
        settings = _settings()
    except ChatbotConfigError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503

    alive_at = _alive_at()
    age = None if not alive_at else max(0, int(time.time() - alive_at))
    return jsonify(
        {
            "ok": True,
            "config_source": settings.source,
            "bot_configured": settings.bot.configured,
            "product_count": len(settings.products),
            "project_count": len(settings.projects),
            "gateway": {
                # 网关每 30 秒写一次心跳，键 TTL 90 秒，超过 90 秒即视为离线。
                "alive": bool(alive_at) and (age is not None and age < 90),
                "last_seen_seconds_ago": age,
            },
        }
    )


@chatbot_bp.route("/projects", methods=["GET"])
def list_projects():
    """返回两级产品目录，并保留扁平项目列表供人工核对。"""
    try:
        settings = _settings()
    except ChatbotConfigError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503

    items = []
    products = []
    for product_index, product in settings.indexed_products():
        product_items = []
        for project_index, project in product.indexed_projects():
            item = {
                "index": len(items) + 1,
                "product_index": product_index,
                "project_index": project_index,
                "product_key": product.key,
                "product_name": product.name,
                "key": project.key,
                "name": project.name,
                "description": project.description,
                "modules": [
                    {"module": module, "branch": branch} for module, branch in project.module_items()
                ],
            }
            product_items.append(item)
            items.append(item)
        products.append(
            {
                "index": product_index,
                "key": product.key,
                "name": product.name,
                "description": product.description,
                "project_count": len(product_items),
                "projects": product_items,
            }
        )
    return jsonify(
        {
            "ok": True,
            "product_total": len(products),
            "total": len(items),
            "products": products,
            "items": items,
        }
    )


@chatbot_bp.route("/questions", methods=["GET"])
def list_questions():
    """分页返回最近的问答记录，默认 20 条。"""
    try:
        limit = min(200, max(1, int(request.args.get("limit") or 20)))
    except (TypeError, ValueError):
        limit = 20
    status = str(request.args.get("status") or "").strip()

    query = ChatbotQuestion.query
    if status:
        query = query.filter(ChatbotQuestion.status == status)
    rows = query.order_by(ChatbotQuestion.id.desc()).limit(limit).all()
    return jsonify({"ok": True, "items": [row.to_dict() for row in rows]})


@chatbot_bp.route("/reload", methods=["POST"])
def reload_settings():
    """重新读取配置文件与机器人凭证；实际生效需要重启网关与 Worker。"""
    try:
        settings = _settings()
    except ChatbotConfigError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify(
        {
            "ok": True,
            "config_source": settings.source,
            "project_count": len(settings.projects),
            "bot_configured": settings.bot.configured,
        }
    )


def _enqueue_knowledge_sync(page_id=None):
    """把同步工作交给专用队列，避免 API 被 Confluence 网络请求阻塞。"""
    payload = {"trigger_source": "page_manual" if page_id else "manual"}
    if page_id:
        payload["page_id"] = str(page_id)
    result = tasks.knowledge_sync_task.apply_async(args=[payload], queue="knowledge-sync")
    return str(result.id or "")


@chatbot_bp.route("/knowledge/status", methods=["GET"])
def knowledge_status():
    """返回知识库配置、索引规模与最近一次同步结果。"""
    try:
        settings = _settings()
    except ChatbotConfigError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    payload = knowledge.knowledge_status()
    payload.update(
        {
            "ok": True,
            "enabled": settings.knowledge.enabled,
            "confluence_configured": settings.confluence.configured,
            "sync_interval_seconds": settings.knowledge.sync_interval_seconds,
        }
    )
    return jsonify(payload)


@chatbot_bp.route("/knowledge/sync", methods=["POST"])
def sync_knowledge():
    """手动投递一次全量 Confluence 同步。"""
    task_id = _enqueue_knowledge_sync()
    return jsonify({"ok": True, "task_id": task_id, "status": "queued"}), 202


@chatbot_bp.route("/knowledge/pages/<page_id>/sync", methods=["POST"])
def sync_knowledge_page(page_id):
    """手动投递单个 Confluence 页面刷新。"""
    task_id = _enqueue_knowledge_sync(str(page_id))
    return jsonify({"ok": True, "task_id": task_id, "status": "queued"}), 202
