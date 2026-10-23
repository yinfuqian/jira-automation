"""routes 模块负责本文件相关的业务流程、数据转换与依赖协作。"""
import hashlib
import os
import subprocess
import threading
import time
from datetime import datetime
from urllib.parse import quote, urlparse, urlunparse

from flask import Blueprint, current_app as app, jsonify, request
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from app.git.gitlab_sync import GitLabSyncError
from app.git.models import GitRef
from app.git.tasks import run_gitlab_sync_once
from extensions import db


git_bp = Blueprint("git", __name__)
_repo_ref_locks = {}
_repo_ref_locks_guard = threading.Lock()


def build_authenticated_git_url(repo_url, username, password):
    """构建并返回 build_authenticated_git_url 对应的业务数据，保持现有调用约定。"""
    parsed = urlparse(repo_url)
    if parsed.scheme not in ("http", "https"):
        return repo_url

    if "@" in parsed.netloc:
        return repo_url

    user = quote(username or "", safe="")
    secret = quote(password or "", safe="")
    auth = f"{user}:{secret}@" if secret else f"{user}@"
    return urlunparse(parsed._replace(netloc=f"{auth}{parsed.netloc}"))


def parse_ls_remote_heads(output):
    """解析或提取并返回 parse_ls_remote_heads 对应的业务数据，保持现有调用约定。"""
    branches = []
    for line in output.splitlines():
        if "\trefs/heads/" not in line:
            continue
        _, ref = line.split("\t", 1)
        branch = ref.removeprefix("refs/heads/").strip()
        if branch:
            branches.append(branch)
    return branches


def parse_ls_remote_refs(output):
    """解析或提取并返回 parse_ls_remote_refs 对应的业务数据，保持现有调用约定。"""
    branches = []
    tags = []

    for line in output.splitlines():
        if "\t" not in line:
            continue

        _, ref = line.split("\t", 1)
        ref = ref.strip()

        if ref.startswith("refs/heads/"):
            branch = ref.removeprefix("refs/heads/").strip()
            if branch and branch not in branches:
                branches.append(branch)
            continue

        if ref.startswith("refs/tags/"):
            tag = ref.removeprefix("refs/tags/").removesuffix("^{}").strip()
            if tag and tag not in tags:
                tags.append(tag)

    return {
        "branches": branches,
        "tags": tags,
        "versions": branches + tags,
    }


def normalize_cached_refs(rows):
    """规范化并返回 normalize_cached_refs 对应的业务数据，保持现有调用约定。"""
    branches = []
    tags = []
    for row in rows or []:
        ref_name = row.ref_name
        if not ref_name:
            continue

        ref_type = row.ref_type or "branch"
        target = tags if ref_type == "tag" else branches
        if ref_name not in target:
            target.append(ref_name)

    return {
        "branches": branches,
        "tags": tags,
        "versions": branches + tags,
    }


def build_git_ref_key(repo_url, ref_type, ref_name):
    """构建并返回 build_git_ref_key 对应的业务数据，保持现有调用约定。"""
    raw_key = "|".join([str(repo_url or ""), str(ref_type or ""), str(ref_name or "")])
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def get_repo_ref_lock(repo_url):
    """读取并返回 get_repo_ref_lock 对应的业务数据，保持现有调用约定。"""
    lock_key = hashlib.sha256(str(repo_url or "").encode("utf-8")).hexdigest()
    with _repo_ref_locks_guard:
        lock = _repo_ref_locks.get(lock_key)
        if lock is None:
            lock = threading.Lock()
            _repo_ref_locks[lock_key] = lock
        return lock


def load_cached_refs(repo_url):
    """读取并返回 load_cached_refs 对应的业务数据，保持现有调用约定。"""
    try:
        rows = (
            GitRef.query.filter_by(repo_url=repo_url)
            .order_by(GitRef.ref_type.asc(), GitRef.id.asc())
            .all()
        )
        refs = normalize_cached_refs(rows)
        return refs if refs["versions"] else None
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.warning("Git ref cache is unavailable, fallback to remote refs", exc_info=True)
        return None


def save_refs_to_cache(repo_url, refs):
    """保存 save_refs_to_cache 对应的业务数据，保持现有调用约定。"""
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            _save_refs_to_cache_once(repo_url, refs)
            return
        except (OperationalError, IntegrityError) as exc:
            db.session.rollback()
            if not is_retryable_cache_write_error(exc) or attempt >= max_attempts:
                app.logger.warning("Failed to save Git refs cache", exc_info=True)
                return
            time.sleep(0.1 * attempt)
        except SQLAlchemyError:
            db.session.rollback()
            app.logger.warning("Failed to save Git refs cache", exc_info=True)
            return


def _save_refs_to_cache_once(repo_url, refs):
    """保存 _save_refs_to_cache_once 对应的业务数据，保持现有调用约定。"""
    now = datetime.utcnow()
    expected = {}
    for ref_type, values in (("branch", refs.get("branches", [])), ("tag", refs.get("tags", []))):
        for ref_name in values or []:
            if not ref_name:
                continue
            ref_key = build_git_ref_key(repo_url, ref_type, ref_name)
            expected[ref_key] = {
                "repo_url": repo_url,
                "ref_name": ref_name,
                "ref_type": ref_type,
            }

    existing_rows = GitRef.query.filter_by(repo_url=repo_url).all()
    existing_by_key = {row.ref_key: row for row in existing_rows}

    for ref_key, values in expected.items():
        cached = existing_by_key.get(ref_key)
        if cached:
            cached.repo_url = values["repo_url"]
            cached.ref_name = values["ref_name"]
            cached.ref_type = values["ref_type"]
            cached.updated_at = now
            continue
        db.session.add(GitRef(
            ref_key=ref_key,
            repo_url=values["repo_url"],
            ref_name=values["ref_name"],
            ref_type=values["ref_type"],
            created_at=now,
            updated_at=now,
        ))

    for cached in existing_rows:
        if cached.ref_key not in expected:
            db.session.delete(cached)
    db.session.commit()


def is_retryable_cache_write_error(exc):
    """判断 is_retryable_cache_write_error 对应的业务数据，保持现有调用约定。"""
    original = getattr(exc, "orig", exc)
    error_code = None
    if getattr(original, "args", None):
        error_code = original.args[0]
    return error_code in {1062, 1205, 1213}


def list_remote_branches(repo_url, username=None, password=None, timeout=30):
    """处理 list_remote_branches 对应的业务步骤，并向调用方返回所需结果。"""
    return list_remote_refs(repo_url, username=username, password=password, timeout=timeout)["branches"]


def list_remote_refs(repo_url, username=None, password=None, timeout=30):
    """处理 list_remote_refs 对应的业务步骤，并向调用方返回所需结果。"""
    authenticated_url = build_authenticated_git_url(repo_url, username, password)
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"

    result = subprocess.run(
        ["git", "ls-remote", "--heads", "--tags", authenticated_url],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        env=env,
    )
    return parse_ls_remote_refs(result.stdout)


@git_bp.route("/branches", methods=["GET"])
def get_remote_branches():
    """读取并返回 get_remote_branches 对应的业务数据，保持现有调用约定。"""
    repo_url = request.args.get("repo_url", "").strip()
    if not repo_url:
        return jsonify({"error": "缺少 repo_url 参数"}), 400

    refresh = request.args.get("refresh", "").strip().lower() in {"1", "true", "yes"}
    cached_only = request.args.get("cached_only", "").strip().lower() in {"1", "true", "yes"}
    cached_refs = load_cached_refs(repo_url)
    if cached_refs and not refresh:
        return jsonify({"repo_url": repo_url, "source": "database", "cached": True, **cached_refs}), 200
    if cached_only:
        return jsonify({
            "repo_url": repo_url,
            "source": "database",
            "cached": False,
            "branches": [],
            "tags": [],
            "versions": [],
        }), 200

    with get_repo_ref_lock(repo_url):
        cached_refs = load_cached_refs(repo_url)
        if cached_refs and not refresh:
            return jsonify({"repo_url": repo_url, "source": "database", **cached_refs}), 200

        try:
            refs = list_remote_refs(
                repo_url,
                username=app.config.get("GIT_USER"),
                password=app.config.get("GIT_PASSWORD"),
            )
            save_refs_to_cache(repo_url, refs)
        except subprocess.TimeoutExpired:
            app.logger.exception("获取远程分支超时: %s", repo_url)
            if cached_refs:
                return jsonify({"repo_url": repo_url, "source": "database", "stale": True, **cached_refs}), 200
            return jsonify({"error": "获取远程分支超时"}), 504
        except subprocess.CalledProcessError as exc:
            app.logger.error("获取远程分支失败: %s, stderr=%s", repo_url, exc.stderr)
            if cached_refs:
                return jsonify({"repo_url": repo_url, "source": "database", "stale": True, **cached_refs}), 200
            return jsonify({"error": "获取远程分支失败，请检查仓库地址或 Git 权限"}), 502

    return jsonify({"repo_url": repo_url, "source": "remote", **refs}), 200


@git_bp.route("/sync-projects", methods=["POST"])
def sync_gitlab_projects():
    """保留给 Windows 客户端的手动同步入口。"""
    try:
        result = run_gitlab_sync_once(app.config)
        if result.get("status") == "skipped" and result.get("reason") == "sync_in_progress":
            return jsonify({
                "error": "Git 项目正在同步中，请稍后重试",
                "code": "GIT_SYNC_IN_PROGRESS",
                **result,
            }), 409
        if result.get("status") == "skipped" and result.get("reason") == "git_base_url_missing":
            return jsonify({
                "error": "未配置 GIT_BASE_URL，无法同步 GitLab 项目",
                "code": "GIT_BASE_URL_MISSING",
                **result,
            }), 503
    except GitLabSyncError as exc:
        app.logger.exception("GitLab 项目同步失败")
        return jsonify({
            "error": str(exc),
            "status_code": exc.status_code,
        }), 502
    except Exception:
        db.session.rollback()
        app.logger.exception("GitLab 项目同步写入数据库失败")
        return jsonify({"error": "GitLab 项目同步写入数据库失败"}), 500

    return jsonify({"message": "GitLab 项目同步完成", **result}), 200

