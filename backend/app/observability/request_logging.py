"""HTTP 请求生命周期日志。"""
import logging
import secrets
import threading
import time

from flask import g, request


REQUEST_LOGGER_NAME = "jira_automation.request"
REQUEST_ID_HEADER = "X-Request-ID"


def install_request_logging(app):
    """记录请求开始、结束、耗时和执行线程，便于定位并发与阻塞问题。"""
    request_logger = logging.getLogger(REQUEST_LOGGER_NAME)

    @app.before_request
    def begin_request_log():
        request_id = getattr(g, "audit_request_id", None)
        if not request_id:
            request_id = f"REQ-{secrets.token_hex(12).upper()}"
        g.log_request_id = request_id
        g.log_request_started_at = time.perf_counter()
        g.log_request_finished = False
        request_logger.info(
            "request_started request_id=%s method=%s path=%s remote_addr=%s thread=%s",
            request_id,
            request.method,
            request.path,
            request.remote_addr or "-",
            threading.current_thread().name,
        )

    @app.after_request
    def finish_request_log(response):
        started_at = getattr(g, "log_request_started_at", None)
        if started_at is None:
            return response

        request_id = getattr(g, "log_request_id", "-")
        duration_ms = max(0, int((time.perf_counter() - started_at) * 1000))
        response.headers.setdefault(REQUEST_ID_HEADER, request_id)
        request_logger.info(
            "request_completed request_id=%s method=%s path=%s status=%s duration_ms=%s thread=%s",
            request_id,
            request.method,
            request.path,
            response.status_code,
            duration_ms,
            threading.current_thread().name,
        )
        g.log_request_finished = True
        return response

    @app.teardown_request
    def finish_failed_request_log(exc):
        if exc is None or getattr(g, "log_request_finished", False):
            return
        request_logger.error(
            "request_failed request_id=%s method=%s path=%s error=%s thread=%s",
            getattr(g, "log_request_id", "-"),
            request.method,
            request.path,
            type(exc).__name__,
            threading.current_thread().name,
            exc_info=exc,
        )

    return request_logger
