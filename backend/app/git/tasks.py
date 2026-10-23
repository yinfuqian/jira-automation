"""GitLab 产品与模块同步的 Celery 任务及启动触发。"""
import logging
import time
from urllib.parse import urlparse

from flask import current_app
from celery.signals import worker_ready
from sqlalchemy.exc import SQLAlchemyError

from app.git.gitlab_sync import GitLabSyncError, sync_gitlab_projects_from_config
from app.git.sync_lock import acquire_sync_lock, release_sync_lock
from extensions import celery, db


GITLAB_SYNC_TASK_NAME = "git.sync_projects"


def run_gitlab_sync_once(config):
    """在共享锁内执行一次 GitLab 同步，供任务、手动接口和启动触发复用。"""
    base_url = str(config.get("GIT_BASE_URL") or "").strip()
    if not base_url:
        return {"status": "skipped", "reason": "git_base_url_missing"}

    started_at = time.perf_counter()
    logging.info(
        "GitLab 项目同步开始：host=%s page_concurrency=%s skip_existing=%s",
        urlparse(base_url).netloc or base_url,
        config.get("GITLAB_SYNC_FETCH_CONCURRENCY", 4),
        config.get("GITLAB_SYNC_SKIP_EXISTING_REPOS", True),
    )
    lock_token = acquire_sync_lock()
    if lock_token is None:
        logging.info("GitLab 项目同步跳过：已有同步任务持有互斥锁")
        return {"status": "skipped", "reason": "sync_in_progress"}

    try:
        stats = sync_gitlab_projects_from_config(config)
    except SQLAlchemyError:
        db.session.rollback()
        raise
    finally:
        release_sync_lock(lock_token)
    logging.info(
        "GitLab 项目同步完成：duration_ms=%s result=%s",
        int((time.perf_counter() - started_at) * 1000),
        stats,
    )
    return {"status": "succeeded", **stats}


@celery.task(
    bind=True,
    name=GITLAB_SYNC_TASK_NAME,
    max_retries=3,
    autoretry_for=(GitLabSyncError, SQLAlchemyError),
    retry_backoff=60,
    retry_jitter=True,
)
def sync_gitlab_projects_task(self):
    """按 Beat 周期或 worker 启动信号执行同步；GitLab 网络错误自动重试。"""
    result = run_gitlab_sync_once(current_app.config)
    if result.get("status") == "skipped":
        logging.info("GitLab 项目同步跳过：%s", result.get("reason"))
    else:
        logging.info("GitLab 项目同步完成：%s", result)
    return result


def install_worker_startup_sync(app, celery_app):
    """worker 每次就绪时入队一次同步；多个进程同时启动由共享锁去重。"""
    if getattr(celery_app, "_gitlab_startup_sync_installed", False):
        return False

    def on_worker_ready(**_kwargs):
        if not app.config.get("GITLAB_SYNC_ENABLED", True):
            return
        if not app.config.get("GITLAB_SYNC_ON_STARTUP", True):
            return
        try:
            sync_gitlab_projects_task.delay()
        except Exception:  # noqa: BLE001 - 启动同步不能阻止 worker 提供服务
            logging.warning("worker 启动时提交 GitLab 项目同步任务失败", exc_info=True)

    if worker_ready is not None:
        worker_ready.connect(on_worker_ready, weak=False)
    celery_app._gitlab_startup_sync_installed = True
    return True
