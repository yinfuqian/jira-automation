"""chatbot_gateway 是智能机器人长连接的独立进程入口。

它不属于 Flask 的请求处理链路，需要单独常驻运行：
    python chatbot_gateway.py

进程内完成三件事：建立并保活长连接、维护 Redis 会话、把问答任务投递给 Celery。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys


# 允许以项目根目录或 backend 目录为工作目录直接执行。
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


def build_logging():
    """按 LOG_LEVEL 配置日志，默认 INFO。"""
    level = str(os.getenv("LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def select_answer_task(settings, answer_mode=""):
    """按本次问题的模式选择队列；未指定模式时沿用知识库开关。"""
    mode = str(answer_mode or "").strip().lower()
    if mode == "knowledge" or (
        not mode and settings.knowledge.enabled and settings.confluence.configured
    ):
        return "chatbot.knowledge_answer", "chatbot-knowledge"
    return "chatbot.answer", "chatbot"


def main():
    """启动网关进程，直到收到退出信号或长连接不可恢复。"""
    build_logging()
    logger = logging.getLogger("chatbot.gateway")

    from app import create_app
    from app.chatbot.session import ChatSessionStore
    from app.chatbot.settings import load_settings
    from app.chatbot.wecom.gateway import ChatbotGateway
    import extensions

    app = create_app()
    with app.app_context():
        config = app.config
        settings = load_settings()

        client = extensions.redis
        if client is None:
            raise RuntimeError("Redis 未初始化，机器人网关无法运行")

        session_store = ChatSessionStore(client, ttl_seconds=settings.session_ttl_seconds)

        def enqueue(payload):
            """把问答任务投递到代码故障分析或 Confluence 知识问答队列。"""
            task_name, queue = select_answer_task(settings, payload.get("answer_mode"))
            extensions.celery.send_task(task_name, args=[payload], queue=queue)

        gateway = ChatbotGateway(
            settings,
            session_store,
            enqueue,
            redis_url=str(config.get("REDIS_URL") or ""),
        )
        logger.info("配置来源：%s", settings.source)
        asyncio.run(_serve(gateway, logger))


async def _serve(gateway, logger):
    """运行网关并处理优雅退出。"""
    task = asyncio.create_task(gateway.run(), name="chatbot-gateway")
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def request_stop():
        """收到 SIGTERM/SIGINT 时停止服务。"""
        logger.info("收到退出信号，正在关闭机器人网关")
        stop_event.set()

    for sig_name in ("SIGTERM", "SIGINT"):
        signal_value = getattr(__import__("signal"), sig_name, None)
        if signal_value is None:
            continue
        try:
            loop.add_signal_handler(signal_value, request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows 事件循环不支持 add_signal_handler，退回默认的 KeyboardInterrupt。
            pass

    done, pending = await asyncio.wait({task}, return_when=asyncio.FIRST_COMPLETED)
    if task in done:
        exc = task.exception()
        if exc is not None:
            logger.exception("机器人网关异常退出：%s", exc)
            raise exc
    for item in pending:
        item.cancel()


if __name__ == "__main__":
    main()
