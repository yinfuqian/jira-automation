"""跨 API、worker 与手动调用共享的 GitLab 同步互斥锁。"""
import threading

from sqlalchemy import text

from extensions import db


GIT_SYNC_LOCK_NAME = "jira_automation:gitlab_sync"
_local_sync_lock = threading.Lock()


def acquire_sync_lock(timeout=0):
    """获取同步锁；MySQL 使用命名锁，其他数据库回退到进程内锁。"""
    bind = db.session.get_bind()
    dialect_name = getattr(getattr(bind, "dialect", None), "name", "")

    if dialect_name.startswith("mysql"):
        connection = db.engine.connect()
        acquired = connection.execute(
            text("SELECT GET_LOCK(:name, :timeout)"),
            {"name": GIT_SYNC_LOCK_NAME, "timeout": timeout},
        ).scalar()
        if acquired == 1:
            return connection
        connection.close()
        return None

    if _local_sync_lock.acquire(blocking=False):
        return _local_sync_lock
    return None


def release_sync_lock(lock_token):
    """释放同步锁并关闭 MySQL 锁连接。"""
    try:
        if hasattr(lock_token, "execute"):
            lock_token.execute(
                text("SELECT RELEASE_LOCK(:name)"),
                {"name": GIT_SYNC_LOCK_NAME},
            )
            return

        if lock_token is _local_sync_lock:
            lock_token.release()
    finally:
        if hasattr(lock_token, "close"):
            lock_token.close()
