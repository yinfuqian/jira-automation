"""  init   模块负责本文件相关的业务流程、数据转换与依赖协作。"""
import logging
import os

from flask import Flask
from flask_cors import CORS

from .config import Config
from .routes import dashboard_bp
from app.analysis.routes.routes import analysis_bp
from app.git.routes import git_bp
from app.logfile.routes.routes import logfile_bp
from app.modules.routes.routes import module_bp
from app.product.routes.routes import product_bp
from app.skillrun.routes.routes import skill_bp
from app.skillrun.tickets import verify_download_ticket
from app.auth.middleware import install_authentication, register_ticket_verifier
from app.auth.account_requests import build_account_request_service
from app.auth.routes import auth_bp
from app.auth.sessions import SessionService
from app.observability.request_logging import install_request_logging
from app.auth.users import UnavailableUserStore, UserStore
from app.audit.service import install_operation_audit
from app.health.routes import health_bp
from extensions import db, migrate, init_redis, init_celery


def create_app(config_overrides=None, redis_client=None):
    """创建并返回 create_app 对应的业务数据，保持现有调用约定。"""
    app = Flask(__name__)
    CORS(app)
    app.config.from_object(Config)
    if config_overrides:
        app.config.update(config_overrides)

    setup_logging(app)

    db.init_app(app)
    migrate.init_app(app, db)

    global redis
    redis = redis_client or init_redis(app)
    init_celery(app)

    try:
        user_store = UserStore(app.config["AUTH_USERS_FILE"])
    except (OSError, ValueError, TypeError) as exc:
        app.logger.error("用户配置初始化失败，业务接口将保持关闭: %s", exc)
        user_store = UnavailableUserStore(exc)
    session_service = SessionService(
        redis,
        user_store,
        max_failures=app.config["AUTH_LOGIN_MAX_FAILURES"],
        failure_window_seconds=app.config["AUTH_LOGIN_WINDOW_SECONDS"],
    )
    app.extensions["auth_users"] = user_store
    app.extensions["auth_sessions"] = session_service
    app.extensions["account_request_service"] = build_account_request_service(app.config)

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(health_bp, url_prefix="/health")
    app.register_blueprint(product_bp, url_prefix="/product")
    app.register_blueprint(module_bp, url_prefix="/module")
    app.register_blueprint(logfile_bp, url_prefix="/logfile")
    app.register_blueprint(analysis_bp, url_prefix="/analysis")
    app.register_blueprint(git_bp, url_prefix="/git")
    app.register_blueprint(skill_bp, url_prefix="/skill")
    app.register_blueprint(dashboard_bp, url_prefix="/dashboard")
    # 交付物下载票据：让 /skill/task/<task_id> 里的 download_url 能直接在浏览器打开。
    register_ticket_verifier("/skill/task/", verify_download_ticket)
    install_operation_audit(app)
    install_request_logging(app)
    install_authentication(app, session_service)

    return app


def setup_logging(app):
    """统一配置文件与容器输出，确保 logging.info 和 app.logger 都能落盘。"""
    log_dir = app.config["LOG_DIR"]
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    level = app.config["LOGGING_LEVEL"]
    formatter = logging.Formatter(app.config["LOGGING_FORMAT"])
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    log_file = app.config["LOGGING_FILE"]
    has_file_handler = any(
        getattr(handler, "_jira_automation_log_path", None) == log_file
        for handler in root_logger.handlers
    )
    if not has_file_handler:
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setLevel(level)
        handler.setFormatter(formatter)
        handler._jira_automation_handler = True
        handler._jira_automation_log_path = log_file
        root_logger.addHandler(handler)

    has_stream_handler = any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        and (handler.level == logging.NOTSET or handler.level <= logging.INFO)
        for handler in root_logger.handlers
    )
    if not has_stream_handler:
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(level)
        stream_handler.setFormatter(formatter)
        stream_handler._jira_automation_handler = True
        stream_handler._jira_automation_stream = True
        root_logger.addHandler(stream_handler)

    # 避免 Flask 默认 handler 与 root handler 重复输出。
    app.logger.handlers.clear()
    app.logger.setLevel(level)
    app.logger.propagate = True
    app.logger.info("启动日志：应用已启动并初始化完成")
