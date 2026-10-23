"""extensions 模块负责本文件相关的业务流程、数据转换与依赖协作。"""
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask import has_app_context
from redis import Redis
from celery import Celery



db = SQLAlchemy()
migrate = Migrate()
redis = None  # 这里不初始化，在 `create_app()` 里初始化
celery = Celery(__name__)

def init_redis(app):
    """根据 Flask 配置初始化 Redis 连接"""
    redis_url = app.config.get("REDIS_URL", "redis://localhost:6379/0")  # 默认 Redis 地址
    return Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_keepalive=True,
        socket_connect_timeout=app.config.get("REDIS_SOCKET_CONNECT_TIMEOUT", 10),
        socket_timeout=app.config.get("REDIS_SOCKET_TIMEOUT", 60),
        retry_on_timeout=True,
        health_check_interval=app.config.get("REDIS_HEALTH_CHECK_INTERVAL", 30),
    )


def init_celery(app):
    """处理 init_celery 对应的业务步骤，并向调用方返回所需结果。"""
    celery.conf.update(
        broker_url=app.config["CELERY_BROKER_URL"],
        result_backend=app.config["CELERY_RESULT_BACKEND"],
        broker_transport_options=app.config.get("CELERY_BROKER_TRANSPORT_OPTIONS", {}),
        result_backend_transport_options=app.config.get("CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS", {}),
        broker_pool_limit=app.config.get("CELERY_BROKER_POOL_LIMIT", 10),
        task_default_queue=app.config.get("CELERY_TASK_DEFAULT_QUEUE", "analysis"),
        task_routes=app.config.get("CELERY_TASK_ROUTES", {}),
        broker_connection_retry=True,
        broker_connection_retry_on_startup=True,
        broker_connection_max_retries=None,
        beat_schedule=app.config.get("CELERY_BEAT_SCHEDULE", {}),
        task_track_started=True,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="Asia/Shanghai",
        enable_utc=False,
    )

    class FlaskContextTask(celery.Task):
        """FlaskContextTask 类封装该领域对象的状态、依赖与相关行为。"""
        def __call__(self, *args, **kwargs):
            """已有应用上下文时直接复用，否则使用 Worker 启动时绑定的应用。"""
            if has_app_context():
                return self.run(*args, **kwargs)
            with app.app_context():
                return self.run(*args, **kwargs)

    celery.Task = FlaskContextTask
    return celery
