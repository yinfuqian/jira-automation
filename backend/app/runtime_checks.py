"""故障分析工具构建期和运行期的统一自检逻辑。

所有检查均返回结构化结果，Docker 构建、健康接口和人工排障可以复用同一套
判断标准。异常消息会先脱敏，避免连接串中的账号和密码进入日志或接口响应。
"""

import importlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _sanitize_message(value):
    """隐藏 URL 和常见键值对中的敏感凭据。"""
    message = str(value or "")
    message = re.sub(r"(\w+://[^:/\s]+:)[^@\s]+(@)", r"\1***\2", message)
    message = re.sub(
        r"(?i)(password|passwd|token|secret|api[_-]?key)\s*[=:]\s*[^\s,;]+",
        r"\1=***",
        message,
    )
    return message


def _run_check(checker, success_message):
    """执行单项检查并记录耗时和脱敏后的失败原因。"""
    started = time.perf_counter()
    try:
        checker()
        return {
            "ok": True,
            "message": success_message,
            "duration_ms": round((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        return {
            "ok": False,
            "message": _sanitize_message(exc),
            "error_type": type(exc).__name__,
            "duration_ms": round((time.perf_counter() - started) * 1000),
        }


def _check_python_runtime():
    """拒绝当前 OCR 依赖尚未支持的 Python 版本。"""
    if sys.version_info[:2] >= (3, 13):
        raise RuntimeError("发布镜像必须使用 Python 3.10，当前版本不支持 OCR 稳定预测")


def _check_dependencies():
    """确认 API、任务队列、数据库、图片和 OCR 核心依赖均可导入。"""
    for module_name in (
        "flask",
        "celery",
        "redis",
        "sqlalchemy",
        "PIL",
        "cv2",
        "numpy",
        "paddle",
        "paddleocr",
    ):
        importlib.import_module(module_name)


class _BuildRedis:
    """构建期创建 Flask 应用时使用的无网络 Redis 替身。"""

    def ping(self):
        """处理 ping 对应的业务步骤，并向调用方返回所需结果。"""
        return True


def _check_application():
    """创建 Flask 应用并确认核心蓝图均已注册。"""
    from app import create_app

    application = create_app(redis_client=_BuildRedis())
    routes = {rule.rule for rule in application.url_map.iter_rules()}
    for required_route in ("/health/live", "/health/ready", "/auth/login", "/analysis/submit_async"):
        if required_route not in routes:
            raise RuntimeError(f"缺少核心路由: {required_route}")


def _check_celery():
    """确认分析任务已注册到 Celery 应用。"""
    from extensions import celery
    from app.analysis.routes import tasks  # noqa: F401 - 导入用于注册任务

    if "analysis.analyze_log_task" not in celery.tasks:
        raise RuntimeError("Celery 未注册 analysis.analyze_log_task")


def _check_git():
    """确认容器内 Git 命令可执行。"""
    completed = subprocess.run(
        ["git", "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if "git version" not in completed.stdout.lower():
        raise RuntimeError("Git 版本检查返回异常")


def _check_java_toolchain():
    """确认容器内置的 JDK 8、JDK 21 与 Maven 3.9.9 都可用，技能才能在容器内编译 Java 代码。"""
    expectations = (
        ("JDK 8", os.getenv("JAVA8_HOME", "/opt/java/jdk8"), "javac 1.8"),
        ("JDK 21", os.getenv("JAVA21_HOME", "/opt/java/jdk21"), "javac 21."),
    )
    for label, home, expected_prefix in expectations:
        completed = subprocess.run(
            [os.path.join(home, "bin", "javac"), "-version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        version = (completed.stdout + completed.stderr).strip()
        if not version.startswith(expected_prefix):
            raise RuntimeError(f"{label} 版本不符合预期：{version}")

    completed = subprocess.run(
        [shutil.which("mvn") or "/opt/maven/bin/mvn", "-version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = (completed.stdout + completed.stderr).strip()
    if "Apache Maven 3.9.9" not in output:
        raise RuntimeError("Maven 版本不符合预期：" + (output.splitlines() or [""])[0])


def _check_ocr():
    """初始化真实 PaddleOCR 模型并完成一次图片预测。"""
    from app.analysis.image_ocr import run_ocr_smoke_check

    result = run_ocr_smoke_check()
    if not result.get("available"):
        raise RuntimeError("；".join(result.get("warnings") or ["OCR 预测不可用"]))


def run_build_checks(
    python_checker=None,
    dependency_checker=None,
    application_checker=None,
    celery_checker=None,
    git_checker=None,
    java_checker=None,
    ocr_checker=None,
):
    """执行不依赖外部 MySQL/Redis 的镜像构建检查。"""
    definitions = {
        "python": (python_checker or _check_python_runtime, "Python 运行时受支持"),
        "dependencies": (dependency_checker or _check_dependencies, "核心依赖导入成功"),
        "application": (application_checker or _check_application, "Flask 应用和路由加载成功"),
        "celery": (celery_checker or _check_celery, "Celery 分析任务注册成功"),
        "git": (git_checker or _check_git, "Git 命令可用"),
        "java": (java_checker or _check_java_toolchain, "JDK 8/JDK 21 与 Maven 3.9.9 可用"),
        "ocr": (ocr_checker or _check_ocr, "PaddleOCR 初始化和预测成功"),
    }
    return {
        name: _run_check(checker, message)
        for name, (checker, message) in definitions.items()
    }


def _check_database():
    """执行轻量 SQL 验证数据库连接。"""
    from sqlalchemy import text
    from extensions import db

    db.session.execute(text("SELECT 1"))


def _check_redis():
    """通过认证会话使用的 Redis 客户端验证连接。"""
    from flask import current_app

    sessions = current_app.extensions.get("auth_sessions")
    if sessions is None:
        raise RuntimeError("认证会话服务尚未初始化")
    sessions.redis.ping()


def _check_users():
    """确认热加载用户 CSV 当前可读取。"""
    from flask import current_app

    user_store = current_app.extensions.get("auth_users")
    if user_store is None or not user_store.available:
        raise RuntimeError("用户 CSV 不可用")


def _check_storage():
    """确认上传、日志、OCR 模型和代码缓存目录可写。"""
    from flask import current_app

    from app.config import DEFAULT_REPO_CACHE_DIR

    paths = (
        current_app.config.get("LOCAL_STORAGE_DIR", "/data/upload"),
        current_app.config.get("LOG_DIR", "/app/app/logs"),
        os.getenv("PADDLE_PDX_CACHE_HOME", "/data/paddle-cache"),
        # 与故障分析、技能共用同一份 REPO_CACHE_DIR 配置，避免自检的目录和实际使用的目录不一致。
        current_app.config.get("REPO_CACHE_DIR") or DEFAULT_REPO_CACHE_DIR,
    )
    for raw_path in paths:
        path = Path(raw_path)
        path.mkdir(parents=True, exist_ok=True)
        if not os.access(path, os.W_OK):
            raise RuntimeError(f"目录不可写: {path}")


def run_readiness_checks(
    database_checker=None,
    redis_checker=None,
    users_checker=None,
    storage_checker=None,
):
    """检查运行中的服务是否已经可以接收业务请求。"""
    definitions = {
        "database": (database_checker or _check_database, "数据库连接正常"),
        "redis": (redis_checker or _check_redis, "Redis 连接正常"),
        "users": (users_checker or _check_users, "用户 CSV 可用"),
        "storage": (storage_checker or _check_storage, "运行目录可写"),
    }
    checks = {
        name: _run_check(checker, message)
        for name, (checker, message) in definitions.items()
    }
    return {"ready": all(item["ok"] for item in checks.values()), "checks": checks}
