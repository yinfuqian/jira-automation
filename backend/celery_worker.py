"""celery worker 模块负责本文件相关的业务流程、数据转换与依赖协作。"""
from app import create_app
from app.git.tasks import install_worker_startup_sync
from app.skillrun.recovery import install_worker_recovery
from extensions import celery


flask_app = create_app()
celery_app = celery

# worker 启动后回收上一次运行遗留的“执行中”任务，避免任务永远停留在 running。
if flask_app.config.get("SKILL_RECOVER_ORPHANS", True):
    install_worker_recovery(flask_app, celery_app)

install_worker_startup_sync(flask_app, celery_app)
