"""routes 模块负责本文件相关的业务流程、数据转换与依赖协作。"""
import base64
import hashlib
import json
import os
import re,subprocess
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Blueprint, request, jsonify, current_app as app
from openai import OpenAI
from celery.result import AsyncResult
from extensions import celery
from app.ai_options import build_ai_request_options
from app.config import DEFAULT_REPO_CACHE_DIR
from app.analysis.routes.tasks import analyze_log_task
from app.analysis.routes.language_adapters import detect_log_languages, extract_source_locations
from app.logfile.models.model import AnalysisKnowledgeCase, Log, QueryRecord
from app.uploads.references import AnalysisInputError, resolve_analysis_inputs
from datetime import datetime
from extensions import db


ISSUE_CATEGORY_LABELS = {
    "code_issue": "\u4ee3\u7801\u95ee\u9898",
    "data_issue": "\u6570\u636e\u95ee\u9898",
    "config_issue": "\u914d\u7f6e\u95ee\u9898",
    "network_issue": "\u7f51\u7edc\u95ee\u9898",
    "dependency_issue": "\u4f9d\u8d56/\u7b2c\u4e09\u65b9\u670d\u52a1\u95ee\u9898",
    "resource_issue": "\u8d44\u6e90\u95ee\u9898",
    "unknown": "\u672a\u77e5",
}

POSSIBLE_CAUSE_KEYS = (
    "code_issue",
    "config_issue",
    "network_issue",
    "data_issue",
    "dependency_issue",
    "resource_issue",
)

DEFAULT_POSSIBLE_CAUSE = {
    "possible": False,
    "confidence": 0,
    "reason": "",
}


class AiServiceError(RuntimeError):
    """AiServiceError 类封装该领域对象的状态、依赖与相关行为。"""
    def __init__(self, message, status_code=None, error_type=None):
        """初始化当前对象的依赖、界面状态或运行参数。"""
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type


class GitCloneError(RuntimeError):
    """GitCloneError 类封装该领域对象的状态、依赖与相关行为。"""
    pass


def _decode_process_output(value):
    """解析或提取并返回 _decode_process_output 对应的业务数据，保持现有调用约定。"""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _sanitize_git_error(value):
    """规范化并返回 _sanitize_git_error 对应的业务数据，保持现有调用约定。"""
    message = _decode_process_output(value).strip()
    return re.sub(r"(https?://)[^/@\s]+(?::[^/@\s]*)?@", r"\1***@", message)


def _build_git_clone_error(address, tag_version, exc):
    """构建并返回 _build_git_clone_error 对应的业务数据，保持现有调用约定。"""
    stderr = _sanitize_git_error(getattr(exc, "stderr", ""))
    detail = stderr.splitlines()[-1].strip() if stderr else "Git 返回状态码 128"
    lowered = detail.lower()
    repo_name = str(address or "").rstrip("/").split("/")[-1].removesuffix(".git")
    if "remote branch" in lowered and "not found" in lowered:
        return GitCloneError(f"仓库 {repo_name} 中不存在分支/Tag {tag_version}")
    if any(signal in lowered for signal in ("repository not found", "authentication failed", "access denied", "403")):
        return GitCloneError(f"仓库 {repo_name} 不存在或当前 Git 账号无权限")
    return GitCloneError(f"仓库 {repo_name} 拉取失败（分支/Tag: {tag_version}）：{detail}")


def normalize_ai_exception(exc):
    """规范化并返回 normalize_ai_exception 对应的业务数据，保持现有调用约定。"""
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    error_text = str(exc or "")
    error_type = ""
    try:
        body = response.json() if response is not None and hasattr(response, "json") else None
    except Exception:
        body = None
    if isinstance(body, dict):
        error_payload = body.get("error") if isinstance(body.get("error"), dict) else body
        error_type = str(error_payload.get("type") or error_payload.get("code") or "")
        error_text = str(error_payload.get("message") or error_text)
    lowered = f"{error_type} {error_text}".lower()
    if status_code == 429 or "usage_limit_reached" in lowered or "usage limit has been reached" in lowered:
        return AiServiceError(
            "AI 服务调用失败：额度已用尽或触发限流，请更换可用 Key、提升额度，或稍后重试。",
            status_code=429,
            error_type=error_type or "usage_limit_reached",
        )
    return AiServiceError(
        f"AI 服务调用失败：{error_text}",
        status_code=status_code,
        error_type=error_type,
    )


def normalize_command_list(value):
    """规范化并返回 normalize_command_list 对应的业务数据，保持现有调用约定。"""
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        items = []

    normalized = []
    for item in items:
        text = str(item or "").strip()
        if text:
            normalized.append(text)
    return normalized

LOG_ERROR_CONTEXT_LINES = 10
LOG_CONTEXT_MAX_CHARS = 30000

COMPONENT_USAGE_PATTERNS = {
    "redis": [
        "redis",
        "redistemplate",
        "stringredistemplate",
        "jedis",
        "lettuce",
        "redisson",
        "@cacheable",
        "@cacheput",
        "@cacheevict",
    ],
    "flyway": [
        "flyway",
        "flywaymigration",
        "db/migration",
        "baselineonmigrate",
        "locations(",
    ],
}

CHAIN_RELEVANCE_PATTERNS = [
    r"https?://[^\s,\"]+",
    r"\bhttp\s*(?:status|code)?\s*[=:]?\s*[45]\d\d\b",
    r"\b(?:status|code)\s*[=:]\s*[45]\d\d\b",
    r"\b(?:feign|dubbo|grpc|rpc|httpclient|resttemplate|webclient|okhttp|requests|httpx)\b",
    r"\b(?:timeout|timed\s*out|connection\s*reset|connection\s*refused|broken\s*pipe)\b",
    r"\b(?:callback|webhook|notify|notification)\b",
    r"(?:调用|请求|回调|通知).{0,20}(?:失败|异常|超时|错误)",
    r"(?:上游|下游|第三方|外部服务|远程服务).{0,20}(?:失败|异常|超时|错误|返回)",
    r"(?:response|响应|返回).{0,20}(?:error|failed|失败|异常|错误|为空|null)",
]

LOCAL_ONLY_PATTERNS = [
    r"\b(?:NullPointerException|IndexOutOfBoundsException|ClassCastException|IllegalArgumentException)\b",
    r"\b(?:ZeroDivisionError|TypeError|ValueError|KeyError|AttributeError)\b",
    r"\bpanic:\s*runtime error\b",
]

RELATED_CODE_SIGNAL_PATTERNS = [
    "http",
    "https",
    "feign",
    "dubbo",
    "grpc",
    "rpc",
    "resttemplate",
    "webclient",
    "okhttp",
    "requests",
    "httpx",
    "callback",
    "webhook",
    "notify",
    "timeout",
    "status",
    "response",
    "return",
]

SOURCE_CODE_SUFFIXES = {
    ".java",
    ".kt",
    ".groovy",
    ".py",
    ".go",
    ".sh",
    ".bash",
    ".zsh",
    ".xml",
    ".yml",
    ".yaml",
    ".properties",
    ".gradle",
}

SKIP_CODE_SEARCH_DIRS = {
    ".git",
    ".idea",
    ".gradle",
    "build",
    "target",
    "out",
    "dist",
    "node_modules",
    "logs",
}


# 配置 Flask Blueprint 和日志格式
analysis_bp = Blueprint("analysis", __name__)
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")

@analysis_bp.route("/branch_get", methods=["POST"])
def get_branch_info():
    """\u83b7\u53d6\u4ed3\u5e93\u7684\u8fdc\u7a0b\u5206\u652f\u4fe1\u606f\u3002"""
    data = request.json
    address = data.get("branchAddress")
    tag_version = data.get("tagVersion")
    if not address or not tag_version:
        logging.error("\u83b7\u53d6\u5206\u652f\u4fe1\u606f\u5931\u8d25\uff1a\u7f3a\u5c11\u5fc5\u8981\u5b57\u6bb5 branchAddress \u6216 tagVersion")
        return jsonify({"error": "\u7f3a\u5c11\u5fc5\u8981\u5b57\u6bb5\uff1abranchAddress, tagVersion"}), 400

    repo_path = clone_git_repo(address, tag_version)
    if not repo_path:
        logging.error("\u83b7\u53d6\u5206\u652f\u4fe1\u606f\u5931\u8d25\uff1a\u514b\u9686 Git \u4ed3\u5e93\u5931\u8d25")
        return jsonify({"error": "\u514b\u9686 Git \u4ed3\u5e93\u5931\u8d25"}), 500

    try:
        logging.info("\u5f00\u59cb\u8bfb\u53d6 Git \u8fdc\u7a0b\u5206\u652f\uff1arepo_path=%s", repo_path)
        result = subprocess.run(
            ["git", "branch", "-r"], cwd=repo_path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        branches = result.stdout.decode("utf-8").splitlines()
        logging.info("\u8bfb\u53d6\u8fdc\u7a0b\u5206\u652f\u6210\u529f\uff1acount=%s", len(branches))
    except subprocess.CalledProcessError as e:
        logging.error("\u8bfb\u53d6\u8fdc\u7a0b\u5206\u652f\u5931\u8d25\uff1a%s", e)
        return jsonify({"error": "\u83b7\u53d6\u5206\u652f\u4fe1\u606f\u5931\u8d25"}), 500

    return jsonify({
        "branches": branches,
        "repo_path": repo_path,
    })


def _repo_cache_dir():
    """返回代码检出缓存根目录：以 REPO_CACHE_DIR 配置为准，脱离应用上下文时用内置默认值。"""
    try:
        return app.config.get("REPO_CACHE_DIR") or DEFAULT_REPO_CACHE_DIR
    except RuntimeError:
        return DEFAULT_REPO_CACHE_DIR


def clone_git_repo(address, tag_version, workspace_id=None):
    """\u514b\u9686\u6307\u5b9a Git \u4ed3\u5e93\u5230\u672c\u5730\u5de5\u4f5c\u76ee\u5f55\u3002"""
    GIT_USER = app.config.get("GIT_USER")
    GIT_PASSWORD = app.config.get("GIT_PASSWORD")

    repo_name = address.split("/")[-1].replace(".git", "")
    safe_tag = re.sub(r"[^A-Za-z0-9_.-]+", "_", tag_version or "default")
    safe_workspace = re.sub(r"[^A-Za-z0-9_.-]+", "_", workspace_id or "shared")
    repo_dir = os.path.join(_repo_cache_dir(), safe_workspace, f"{repo_name}-{safe_tag}")

    if os.path.exists(repo_dir):
        logging.info("Git \u4ed3\u5e93\u5df2\u5b58\u5728\uff0c\u590d\u7528\u672c\u5730\u76ee\u5f55\uff1arepo_dir=%s", repo_dir)
        return repo_dir

    repo_address = address.replace("https://", f"https://{GIT_USER}:{GIT_PASSWORD}@")
    try:
        os.makedirs(os.path.dirname(repo_dir), exist_ok=True)
        logging.info(
            "\u5f00\u59cb\u514b\u9686 Git \u4ed3\u5e93\uff1arepo=%s, version=%s, workspace=%s",
            address,
            tag_version,
            workspace_id or "shared",
        )
        subprocess.run(
            ["git", "clone", "-b", tag_version, repo_address, repo_dir],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        logging.info("Git \u4ed3\u5e93\u514b\u9686\u6210\u529f\uff1arepo_dir=%s", repo_dir)
        return repo_dir
    except subprocess.CalledProcessError as e:
        clone_error = _build_git_clone_error(address, tag_version, e)
        logging.error("Git clone failed: repo=%s, version=%s, reason=%s", address, tag_version, clone_error)
        if os.path.isdir(repo_dir):
            shutil.rmtree(repo_dir, ignore_errors=True)
        raise clone_error from e


def _repo_address_with_credentials(address):
    """返回带 Git 凭据的仓库地址；凭据不写日志也不返回给调用方。"""
    git_user = app.config.get("GIT_USER")
    git_password = app.config.get("GIT_PASSWORD")
    if git_user and git_password and str(address).startswith("https://"):
        return str(address).replace(
            "https://",
            f"https://{git_user}:{git_password}@",
            1,
        )
    return str(address or "")


def inspect_git_tag(address, tag_version, timeout=30):
    """检查远端仓库中是否存在精确 Tag，返回结构化结果而不抛出业务异常。"""
    tag = str(tag_version or "").strip()
    if not address or not tag:
        return {"exists": False, "reason": "缺少仓库地址或 Tag"}

    try:
        result = subprocess.run(
            [
                "git",
                "ls-remote",
                "--tags",
                "--refs",
                _repo_address_with_credentials(address),
                f"refs/tags/{tag}",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"exists": False, "reason": f"校验 Tag 超时：{tag}"}
    except OSError as exc:
        return {"exists": False, "reason": f"无法执行 Git Tag 校验：{exc}"}

    if result.returncode != 0:
        stderr = _sanitize_git_error(getattr(result, "stderr", ""))
        detail = stderr.splitlines()[-1].strip() if stderr else f"Git 返回状态码 {result.returncode}"
        return {"exists": False, "reason": detail}

    exists = bool(_decode_process_output(getattr(result, "stdout", "")).strip())
    return {
        "exists": exists,
        "reason": "" if exists else f"仓库中不存在精确 Tag：{tag}",
    }


def validate_analysis_repositories(repositories, tag_checker=None):
    """校验分析用主模块和关联模块的精准 Tag 均真实存在。"""
    checker = tag_checker or inspect_git_tag
    missing = []
    skipped_common = []
    for repository in repositories or []:
        module_name = str(repository.get("moduleName") or repository.get("moduleId") or "unknown")
        address = str(repository.get("branchAddress") or "").strip()
        tag_version = str(repository.get("tagVersion") or "").strip()
        if repository.get("common") and not tag_version:
            if not address:
                missing.append({
                    "moduleName": module_name,
                    "branchAddress": address,
                    "tagVersion": tag_version,
                    "reason": "公共组件缺少仓库地址",
                })
            else:
                skipped_common.append({"moduleName": module_name, "branchAddress": address})
            continue
        if not address or not tag_version:
            missing.append({
                "moduleName": module_name,
                "branchAddress": address,
                "tagVersion": tag_version,
                "reason": "缺少仓库地址或精准 Tag",
            })
            continue

        try:
            checked = checker(address, tag_version)
        except Exception as exc:
            checked = {"exists": False, "reason": str(exc)}
        if isinstance(checked, bool):
            checked = {"exists": checked, "reason": "" if checked else f"仓库中不存在精确 Tag：{tag_version}"}
        if not checked.get("exists"):
            missing.append({
                "moduleName": module_name,
                "branchAddress": address,
                "tagVersion": tag_version,
                "reason": checked.get("reason") or f"仓库中不存在精确 Tag：{tag_version}",
            })

    return {
        "ok": not missing,
        "checked": len(repositories or []),
        "skippedCommon": skipped_common,
        "missing": missing,
    }


@analysis_bp.route("/validate_refs", methods=["POST"])
def validate_analysis_refs():
    """在提交异步分析前校验全部精准 Tag，避免进入克隆阶段后才发现。"""
    data = request.json or {}
    repositories = build_analysis_repositories(data)
    return jsonify(validate_analysis_repositories(repositories))


@analysis_bp.route("/submit_async", methods=["POST"])
def submit_async_analysis():
    """提交 submit_async_analysis 对应的业务数据，保持现有调用约定。"""
    data = request.json or {}
    source_type = str(data.get("source_type") or "file").lower()
    if source_type == "image" and not data.get("image_tag"):
        return jsonify({"error": "\u7f3a\u5c11\u5fc5\u8981\u5b57\u6bb5", "missing_fields": ["image_tag"]}), 400

    def lookup_log(reference):
        """查找或推断并返回 lookup_log 对应的业务数据，保持现有调用约定。"""
        if isinstance(reference, int) or str(reference).isdigit():
            return db.session.get(Log, int(reference))
        return Log.query.filter_by(log_file_path=str(reference)).first()

    try:
        resolve_analysis_inputs(data, app.config.get("LOCAL_STORAGE_DIR", "/data/upload"), lookup_log)
    except AnalysisInputError as exc:
        return jsonify({"error": str(exc)}), 400

    required_fields = ["productId", "moduleId", "branchAddress", "tagVersion", "file_path"]
    missing_fields = [field for field in required_fields if not data.get(field)]
    if missing_fields:
        return jsonify({"error": "\u7f3a\u5c11\u5fc5\u8981\u5b57\u6bb5", "missing_fields": missing_fields}), 400

    task = analyze_log_task.delay(data)
    return jsonify({
        "task_id": task.id,
        "state": task.state,
        "status_url": f"/analysis/task/{task.id}",
    }), 202


@analysis_bp.route("/task/<task_id>", methods=["GET"])
def get_analysis_task(task_id):
    """读取并返回 get_analysis_task 对应的业务数据，保持现有调用约定。"""
    task = AsyncResult(task_id, app=celery)
    payload = {
        "task_id": task_id,
        "state": task.state,
        "ready": task.ready(),
        "successful": task.successful() if task.ready() else False,
    }
    progress = task.info if isinstance(task.info, dict) else None
    if progress:
        payload["progress"] = progress

    if task.ready():
        if task.successful():
            payload["result"] = task.result
        else:
            payload["error"] = str(task.result)

    return jsonify(payload)


@analysis_bp.route("/task/<task_id>/cancel", methods=["POST"])
def cancel_analysis_task(task_id):
    """取消 cancel_analysis_task 对应的业务数据，保持现有调用约定。"""
    celery.control.revoke(task_id, terminate=True, signal="SIGTERM")
    return jsonify({
        "task_id": task_id,
        "state": "REVOKED",
        "message": "\u4efb\u52a1\u5df2\u8bf7\u6c42\u53d6\u6d88",
    }), 202

@analysis_bp.route("/log_analysis", methods=["POST"])
def analyze_log_and_code():
    """执行故障分析并返回 analyze_log_and_code 对应的业务数据，保持现有调用约定。"""
    logging.info("==> \u8fdb\u5165 analyze_log_and_code")
    data = request.json
    logging.info("\u6536\u5230\u540c\u6b65\u5206\u6790\u8bf7\u6c42\uff1a%s", data)
    log_file_path = data.get("file_path")
    repo_path = data.get("repo_path")

    logging.info("\u65e5\u5fd7\u6587\u4ef6\u8def\u5f84\uff1a%s", log_file_path)
    logging.info("\u4ee3\u7801\u4ed3\u5e93\u8def\u5f84\uff1a%s", repo_path)

    if not log_file_path or not os.path.exists(log_file_path):
        logging.error("\u65e5\u5fd7\u6587\u4ef6\u8def\u5f84\u65e0\u6548\u6216\u6587\u4ef6\u4e0d\u5b58\u5728\uff1a%s", log_file_path)
        return jsonify({"error": "\u65e5\u5fd7\u6587\u4ef6\u8def\u5f84\u65e0\u6548\u6216\u6587\u4ef6\u4e0d\u5b58\u5728"}), 400
    if not repo_path or not os.path.exists(repo_path):
        logging.error("\u4ed3\u5e93\u8def\u5f84\u65e0\u6548\u6216\u76ee\u5f55\u4e0d\u5b58\u5728\uff1a%s", repo_path)
        return jsonify({"error": "\u4ed3\u5e93\u8def\u5f84\u65e0\u6548\u6216\u76ee\u5f55\u4e0d\u5b58\u5728"}), 400

    try:
        with open(log_file_path, "r", encoding="utf-8") as file_obj:
            log_content = file_obj.read()
        logging.info("\u65e5\u5fd7\u6587\u4ef6\u8bfb\u53d6\u6210\u529f\uff1apath=%s, chars=%s", log_file_path, len(log_content))
    except Exception as exc:
        logging.exception("\u8bfb\u53d6\u65e5\u5fd7\u5931\u8d25\uff1a%s", exc)
        return jsonify({"error": "\u8bfb\u53d6\u65e5\u5fd7\u5931\u8d25"}), 500

    logging.info("\u5f00\u59cb\u6267\u884c\u65e5\u5fd7\u521d\u6b65\u5206\u6790")
    try:
        log_analysis = build_backend_log_analysis(log_content)
    except AiServiceError as exc:
        insert_query_record(data, 1, status="ai_service_failed")
        return jsonify({"error": str(exc), "error_type": exc.error_type}), exc.status_code or 500
    if not log_analysis:
        logging.error("\u65e5\u5fd7\u521d\u6b65\u5206\u6790\u8fd4\u56de\u4e3a\u7a7a")
        insert_query_record(data, 1)
        return jsonify({"error": "\u65e5\u5fd7\u5206\u6790\u5931\u8d25"}), 500
    logging.info("\u65e5\u5fd7\u521d\u6b65\u5206\u6790\u5b8c\u6210")

    logging.info("\u5f00\u59cb\u4ece\u65e5\u5fd7\u4e2d\u63d0\u53d6\u9519\u8bef\u5806\u6808\u548c\u5f02\u5e38\u4fe1\u606f")
    error_info = extract_error_info_from_log(log_content)
    if not error_info:
        logging.error("\u672a\u80fd\u4ece\u65e5\u5fd7\u4e2d\u63d0\u53d6\u5230\u9519\u8bef\u4fe1\u606f")
        return jsonify({"error": "\u65e0\u6cd5\u4ece\u65e5\u5fd7\u4e2d\u63d0\u53d6\u9519\u8bef\u4fe1\u606f"}), 400
    logging.info("\u9519\u8bef\u4fe1\u606f\u63d0\u53d6\u5b8c\u6210\uff1acount=%s", len(error_info))

    logging.info("\u5f00\u59cb\u89e3\u6790\u4ed3\u5e93\u4e2d\u7684\u5b9e\u9645\u4ee3\u7801\u6587\u4ef6\u8def\u5f84")
    resolved_errors = resolve_file_paths(repo_path, error_info)
    logging.info("\u4ee3\u7801\u6587\u4ef6\u8def\u5f84\u89e3\u6790\u5b8c\u6210\uff1acount=%s", len(resolved_errors))

    logging.info("\u5f00\u59cb\u63d0\u53d6\u76f8\u5173\u4ee3\u7801\u7247\u6bb5")
    stack_snippets = extract_code_snippets(resolved_errors)
    components = detect_error_components(log_content)
    component_snippets = find_component_code_usages(repo_path, components)
    code_snippets = merge_code_snippets(stack_snippets, component_snippets)
    code_findings = build_code_findings(code_snippets)
    logging.info(
        "\u4ee3\u7801\u7247\u6bb5\u63d0\u53d6\u5b8c\u6210\uff1astack_snippets=%s, component_snippets=%s, merged_snippets=%s",
        len(stack_snippets),
        len(component_snippets),
        len(code_snippets),
    )

    logging.info("\u5f00\u59cb\u6267\u884c\u4ee3\u7801\u4e0e\u65e5\u5fd7\u7684\u7efc\u5408\u5206\u6790")
    try:
        code_analysis = analyze_code_with_deepseek(log_content, log_analysis, code_snippets)
    except AiServiceError as exc:
        insert_query_record(data, 1, status="ai_service_failed")
        return jsonify({"error": str(exc), "error_type": exc.error_type}), exc.status_code or 500
    if code_analysis:
        insert_query_record(data, 0)
        logging.info("\u7efc\u5408\u5206\u6790\u6210\u529f")
    else:
        logging.error("\u7efc\u5408\u5206\u6790\u5931\u8d25")
        insert_query_record(data, 1)
    logging.info("\u540c\u6b65\u5206\u6790\u5b8c\u6210\uff0c\u51c6\u5907\u8fd4\u56de\u7ed3\u679c")
    response_payload = {
        "log_analysis": log_analysis,
        "code_snippets": code_snippets,
        "code_findings": code_findings,
        "code_analysis": code_analysis
    }
    return jsonify(response_payload)


def normalize_error_message(message):
    """规范化并返回 normalize_error_message 对应的业务数据，保持现有调用约定。"""
    text = str(message or "").lower()
    text = re.sub(r"\d{4}-\d{2}-\d{2}[ t]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?", "<timestamp>", text)
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<uuid>", text)
    text = re.sub(r"\b[a-z0-9_-]*id\s*[:=]\s*[a-z0-9_.:-]+\b", "id=<id>", text)
    text = re.sub(r"\b\d+\b", "<num>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def build_error_fingerprint(
    product_id,
    module_id,
    error_info,
    branch_url=None,
    branch_version=None,
    fallback_text=None,
    grouped_log_errors=None,
    related_modules=None,
):
    """构建并返回 build_error_fingerprint 对应的业务数据，保持现有调用约定。"""
    first_error = (error_info or [{}])[0] or {}
    raw_error = first_error.get("error") or ""
    if not raw_error and fallback_text:
        raw_error = str(fallback_text)
    error_type = extract_error_type(raw_error)
    normalized_message = normalize_error_message(raw_error)
    file_name = first_error.get("file") or ""
    group_signatures = sorted({
        str(group.get("signature") or "")
        for group in (grouped_log_errors or [])
        if isinstance(group, dict) and group.get("signature")
    })
    related_repository_versions = sorted(
        (
            str(module.get("role") or "related"),
            str(module.get("moduleName") or module.get("moduleId") or ""),
            str(module.get("branchAddress") or ""),
            str(module.get("tagVersion") or ""),
        )
        for module in (related_modules or [])
        if isinstance(module, dict)
        and module.get("branchAddress")
        and module.get("tagVersion")
    )
    seed = "|".join([
        (
            "grouped-errors-v3-related"
            if related_repository_versions
            else ("grouped-errors-v2" if group_signatures else "legacy-error-v1")
        ),
        str(product_id or ""),
        str(module_id or ""),
        str(branch_url or ""),
        str(branch_version or ""),
        str(error_type or ""),
        str(file_name or ""),
        normalized_message,
        safe_json_dumps(group_signatures),
        safe_json_dumps(related_repository_versions),
    ])
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def extract_error_type(error_message):
    """解析或提取并返回 extract_error_type 对应的业务数据，保持现有调用约定。"""
    match = re.search(r"([A-Za-z_][\w\.]*(?:Exception|Error))", str(error_message or ""))
    return match.group(1) if match else None


def clamp_confidence(value):
    """规范化并返回 clamp_confidence 对应的业务数据，保持现有调用约定。"""
    try:
        confidence = float(value or 0)
    except (TypeError, ValueError):
        confidence = 0
    return max(0, min(1, confidence))


def normalize_possible_causes(value):
    """规范化并返回 normalize_possible_causes 对应的业务数据，保持现有调用约定。"""
    normalized = {}
    source = value if isinstance(value, dict) else {}
    for key in POSSIBLE_CAUSE_KEYS:
        item = source.get(key) if isinstance(source.get(key), dict) else {}
        normalized[key] = {
            "possible": bool(item.get("possible", DEFAULT_POSSIBLE_CAUSE["possible"])),
            "confidence": clamp_confidence(item.get("confidence", DEFAULT_POSSIBLE_CAUSE["confidence"])),
            "reason": str(item.get("reason", DEFAULT_POSSIBLE_CAUSE["reason"]) or "").strip(),
        }
    return normalized


def normalize_code_locations(value):
    """规范化并返回 normalize_code_locations 对应的业务数据，保持现有调用约定。"""
    if not isinstance(value, list):
        return []
    locations = []
    for item in value:
        if not isinstance(item, dict):
            continue
        file_path = str(item.get("file") or item.get("file_path") or "").strip()
        line = item.get("line")
        try:
            line = int(line) if line not in (None, "") else None
        except (TypeError, ValueError):
            line = None
        if not file_path and line is None:
            continue
        locations.append({
            "file": file_path,
            "line": line,
            "reason": str(item.get("reason") or "").strip(),
        })
    return locations


def normalize_issue_items(value):
    """规范化并返回 normalize_issue_items 对应的业务数据，保持现有调用约定。"""
    if not isinstance(value, list):
        return []
    issues = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            continue
        issue_category = item.get("issue_category") or item.get("category") or "unknown"
        if issue_category not in ISSUE_CATEGORY_LABELS:
            issue_category = "unknown"
        evidence = item.get("evidence") or []
        if isinstance(evidence, str):
            evidence = [evidence]
        elif not isinstance(evidence, list):
            evidence = []
        query_commands = (
            item.get("query_commands")
            or item.get("query_command")
            or item.get("query_commond")
        )
        fix_commands = (
            item.get("fix_commands")
            or item.get("fix_command")
            or item.get("fix_commond")
        )
        issues.append({
            "index": index,
            "title": str(item.get("title") or item.get("issue_title") or f"问题{index}").strip(),
            "issue_category": issue_category,
            "issue_category_label": ISSUE_CATEGORY_LABELS[issue_category],
            "summary": str(item.get("summary") or item.get("conclusion_summary") or "").strip(),
            "root_cause": str(item.get("root_cause") or "").strip(),
            "solution": str(item.get("solution") or "").strip(),
            "confidence": clamp_confidence(item.get("confidence")),
            "evidence": [str(entry) for entry in evidence if str(entry).strip()],
            "query_commands": normalize_command_list(query_commands),
            "fix_commands": normalize_command_list(fix_commands),
            "code_locations": normalize_code_locations(item.get("code_locations") or item.get("code_snippets")),
        })
    return issues


def parse_issue_conclusion(ai_text):
    """解析或提取并返回 parse_issue_conclusion 对应的业务数据，保持现有调用约定。"""
    raw_text = str(ai_text or "").strip()
    payload = extract_json_object(raw_text)
    if not isinstance(payload, dict):
        return {
            "issue_category": "unknown",
            "issue_category_label": ISSUE_CATEGORY_LABELS["unknown"],
            "conclusion_summary": "",
            "root_cause": "",
            "solution": "",
            "confidence": 0,
            "possible_causes": normalize_possible_causes(None),
            "query_commands": [],
            "fix_commands": [],
            "evidence": [],
            "issues": [],
            "issue_count": 0,
            "raw_analysis": raw_text,
        }

    issue_category = payload.get("issue_category") or "unknown"
    if issue_category not in ISSUE_CATEGORY_LABELS:
        issue_category = "unknown"
    confidence = clamp_confidence(payload.get("confidence"))
    evidence = payload.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    elif not isinstance(evidence, list):
        evidence = []
    issue_items = (
        payload.get("issues")
        or payload.get("issue_details")
        or payload.get("problem_details")
        or payload.get("problems")
    )
    top_level_query_commands = (
        payload.get("query_commands")
        or payload.get("query_command")
        or payload.get("query_commond")
    )
    top_level_fix_commands = (
        payload.get("fix_commands")
        or payload.get("fix_command")
        or payload.get("fix_commond")
    )
    issues = normalize_issue_items(issue_items)
    if not issues and any(payload.get(key) for key in ("conclusion_summary", "root_cause", "solution", "evidence")):
        issues = normalize_issue_items([{
            "title": payload.get("conclusion_summary") or "综合问题",
            "issue_category": issue_category,
            "summary": payload.get("conclusion_summary"),
            "root_cause": payload.get("root_cause"),
            "solution": payload.get("solution"),
            "confidence": confidence,
            "evidence": evidence,
            "query_commands": top_level_query_commands,
            "fix_commands": top_level_fix_commands,
            "code_locations": payload.get("code_locations"),
        }])

    return {
        "issue_category": issue_category,
        "issue_category_label": ISSUE_CATEGORY_LABELS[issue_category],
        "conclusion_summary": str(payload.get("conclusion_summary") or "").strip(),
        "root_cause": str(payload.get("root_cause") or "").strip(),
        "solution": str(payload.get("solution") or "").strip(),
        "confidence": confidence,
        "possible_causes": normalize_possible_causes(payload.get("possible_causes")),
        "query_commands": normalize_command_list(top_level_query_commands),
        "fix_commands": normalize_command_list(top_level_fix_commands),
        "evidence": [str(item) for item in evidence],
        "issues": issues,
        "issue_count": len(issues),
        "raw_analysis": raw_text,
    }


def extract_json_object(text):
    """解析或提取并返回 extract_json_object 对应的业务数据，保持现有调用约定。"""
    cleaned = re.sub(r"^\s*```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    candidates = [cleaned]
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
    return None


def find_knowledge_case(product_id, module_id, error_fingerprint):
    """查找或推断并返回 find_knowledge_case 对应的业务数据，保持现有调用约定。"""
    if not all([product_id, module_id, error_fingerprint]):
        return None
    return AnalysisKnowledgeCase.query.filter_by(
        product_id=int(product_id),
        module_id=int(module_id),
        error_fingerprint=error_fingerprint,
    ).first()


def build_cached_analysis_payload(case, task_id=None, repo_path=None):
    """构建并返回 build_cached_analysis_payload 对应的业务数据，保持现有调用约定。"""
    case.hit_count = (case.hit_count or 0) + 1
    case.last_hit_at = datetime.utcnow()
    db.session.commit()

    code_snippets = safe_json_loads(case.code_snippets, [])
    code_findings = build_code_findings(code_snippets)
    evidence = safe_json_loads(case.evidence, [])
    cached_payload = extract_json_object(case.ai_analysis or "") or {}
    cached_issues = normalize_issue_items(cached_payload.get("issues"))
    issue_conclusion = {
        "issue_category": case.issue_category,
        "issue_category_label": ISSUE_CATEGORY_LABELS.get(case.issue_category, ISSUE_CATEGORY_LABELS["unknown"]),
        "conclusion_summary": case.conclusion_summary or "",
        "root_cause": case.root_cause or "",
        "solution": case.solution or "",
        "confidence": float(case.confidence or 0),
        "possible_causes": normalize_possible_causes(cached_payload.get("possible_causes")),
        "query_commands": normalize_command_list(cached_payload.get("query_commands")),
        "fix_commands": normalize_command_list(cached_payload.get("fix_commands")),
        "evidence": evidence,
        "issues": cached_issues,
        "issue_count": len(cached_issues),
        "raw_analysis": case.ai_analysis or "",
    }
    return {
        "task_id": task_id,
        "repo_path": repo_path,
        "knowledge_hit": True,
        "knowledge_case_id": case.id,
        "knowledge_hit_count": case.hit_count,
        "error_fingerprint": case.error_fingerprint,
        "log_analysis": case.log_excerpt or "",
        "code_snippets": code_snippets,
        "code_findings": code_findings,
        "analysis_evidence": {
            "used_code_context": bool(code_snippets),
            "log_error_event_count": len(cached_issues) or (1 if case.error_message else 0),
            "error_info_count": 1 if case.error_message else 0,
            "resolved_file_count": len(safe_json_loads(case.code_files, [])),
            "code_snippet_count": len(code_snippets),
            "code_snippet_files": safe_json_loads(case.code_files, []),
        },
        "issue_conclusion": issue_conclusion,
        "code_analysis": case.ai_analysis or case.conclusion_summary or "",
    }


def safe_json_dumps(value):
    """处理 safe_json_dumps 对应的业务步骤，并向调用方返回所需结果。"""
    return json.dumps(value if value is not None else [], ensure_ascii=False)


def safe_json_loads(value, default):
    """处理 safe_json_loads 对应的业务步骤，并向调用方返回所需结果。"""
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def upsert_knowledge_case(data, error_fingerprint, error_info, log_content, code_snippets, code_analysis, issue_conclusion):
    """处理 upsert_knowledge_case 对应的业务步骤，并向调用方返回所需结果。"""
    if not error_fingerprint or not code_analysis:
        return None
    product_id = int(data.get("productId"))
    module_id = int(data.get("moduleId"))
    case = find_knowledge_case(product_id, module_id, error_fingerprint)
    first_error = (error_info or [{}])[0] or {}
    code_files = []
    for snippet in code_snippets or []:
        file_path = snippet.get("file") if isinstance(snippet, dict) else None
        if file_path and file_path not in code_files:
            code_files.append(file_path)
    if not case:
        case = AnalysisKnowledgeCase(
            product_id=product_id,
            module_id=module_id,
            error_fingerprint=error_fingerprint,
            hit_count=0,
        )
        db.session.add(case)

    case.error_type = extract_error_type(first_error.get("error"))
    case.error_message = first_error.get("error")
    case.stack_top_file = first_error.get("file")
    case.stack_top_line = first_error.get("line")
    case.branch_url = data.get("branchAddress")
    case.branch_version = data.get("tagVersion")
    case.log_excerpt = (log_content or "")[:4000]
    case.code_files = safe_json_dumps(code_files)
    case.code_snippets = safe_json_dumps(code_snippets or [])
    case.issue_category = issue_conclusion.get("issue_category") or "unknown"
    case.conclusion_summary = issue_conclusion.get("conclusion_summary")
    case.root_cause = issue_conclusion.get("root_cause")
    case.solution = issue_conclusion.get("solution")
    case.ai_analysis = code_analysis
    case.evidence = safe_json_dumps(issue_conclusion.get("evidence") or [])
    case.confidence = issue_conclusion.get("confidence") or 0
    case.updated_at = datetime.utcnow()
    db.session.commit()
    return case


def get_int_config(name, default):
    """读取并返回 get_int_config 对应的业务数据，保持现有调用约定。"""
    try:
        return int(app.config.get(name, default))
    except (TypeError, ValueError):
        return default


def get_error_context_settings():
    """读取并返回 get_error_context_settings 对应的业务数据，保持现有调用约定。"""
    context_lines = get_int_config("LOG_ERROR_CONTEXT_LINES", LOG_ERROR_CONTEXT_LINES)
    max_chars = get_int_config("LOG_CONTEXT_MAX_CHARS", LOG_CONTEXT_MAX_CHARS)
    return max(0, context_lines), max(1000, max_chars)


def extract_relevant_log_context(log_content, context_lines=None, max_chars=None):
    """解析或提取并返回 extract_relevant_log_context 对应的业务数据，保持现有调用约定。"""
    text = str(log_content or "")
    if not text:
        return ""

    lines = text.splitlines()
    if not lines:
        return text

    if context_lines is None or max_chars is None:
        default_context_lines, default_max_chars = get_error_context_settings()
        context_lines = default_context_lines if context_lines is None else context_lines
        max_chars = default_max_chars if max_chars is None else max_chars

    context_lines = max(0, int(context_lines))
    max_chars = max(1000, int(max_chars))
    critical_re = re.compile(
        r"(error|exception|traceback|caused by|failed|failure|timeout|reset by peer|connection reset|refused|unavailable|nullpointer|broken pipe)",
        re.IGNORECASE,
    )
    stack_re = re.compile(r"^\s+(at\s+[\w.$]+\(|File\s+\"|\.\.\.\s+\d+\s+more)")

    selected_indexes = set()
    anchor_indexes = [index for index, line in enumerate(lines) if critical_re.search(line)]
    for index in anchor_indexes:
        for nearby in range(max(0, index - context_lines), min(len(lines), index + context_lines + 1)):
            selected_indexes.add(nearby)

    # Keep stack continuation lines following selected error ranges, even if they exceed the fixed window a little.
    for index in list(selected_indexes):
        cursor = index + 1
        while cursor < len(lines) and stack_re.search(lines[cursor]):
            selected_indexes.add(cursor)
            cursor += 1

    if not selected_indexes:
        warning_indexes = [index for index, line in enumerate(lines) if re.search(r"\bwarn(?:ing)?\b", line, re.IGNORECASE)]
        for index in warning_indexes[:5]:
            for nearby in range(max(0, index - context_lines), min(len(lines), index + context_lines + 1)):
                selected_indexes.add(nearby)

    if not selected_indexes:
        selected_indexes.update(range(min(len(lines), context_lines * 2 + 1 or 20)))

    excerpt_lines = []
    current_len = 0
    previous_index = None
    sorted_indexes = sorted(selected_indexes)
    truncated = False
    for index in sorted_indexes:
        if previous_index is not None and index > previous_index + 1:
            separator = f"... skipped {index - previous_index - 1} lines ..."
            if current_len + len(separator) + 1 <= max_chars:
                excerpt_lines.append(separator)
                current_len += len(separator) + 1
        numbered_line = f"{index + 1}: {lines[index]}"
        if current_len + len(numbered_line) + 1 > max_chars and excerpt_lines:
            excerpt_lines.append("... truncated by LOG_CONTEXT_MAX_CHARS ...")
            truncated = True
            break
        excerpt_lines.append(numbered_line)
        current_len += len(numbered_line) + 1
        previous_index = index

    if truncated:
        return build_head_tail_log_context(lines, sorted_indexes, max_chars)

    return "\n".join(excerpt_lines)


def build_head_tail_log_context(lines, sorted_indexes, max_chars):
    """构建并返回 build_head_tail_log_context 对应的业务数据，保持现有调用约定。"""
    marker = "... truncated middle by LOG_CONTEXT_MAX_CHARS; preserved first and last error batches ..."
    half_budget = max(200, (max_chars - len(marker) - 2) // 2)
    head = []
    head_len = 0
    for index in sorted_indexes:
        numbered_line = f"{index + 1}: {lines[index]}"
        if head and head_len + len(numbered_line) + 1 > half_budget:
            break
        head.append((index, numbered_line))
        head_len += len(numbered_line) + 1

    tail = []
    tail_len = 0
    used_head_indexes = {index for index, _ in head}
    for index in reversed(sorted_indexes):
        if index in used_head_indexes:
            continue
        numbered_line = f"{index + 1}: {lines[index]}"
        if tail and tail_len + len(numbered_line) + 1 > half_budget:
            break
        tail.append((index, numbered_line))
        tail_len += len(numbered_line) + 1
    tail.reverse()

    rendered = [line for _, line in head]
    if tail:
        rendered.append(marker)
        rendered.extend(line for _, line in tail)
    return "\n".join(rendered)


def _is_log_record_start(line):
    """判断 _is_log_record_start 对应的业务数据，保持现有调用约定。"""
    return bool(re.match(r"^\s*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", line or ""))


def _is_error_event_start(line):
    """判断 _is_error_event_start 对应的业务数据，保持现有调用约定。"""
    text = line or ""
    stripped = text.strip()
    if not stripped:
        return False
    if re.search(r"\b(ERROR|FATAL)\b", text):
        return True
    if re.match(r"^(Traceback \(most recent call last\)|panic:\s+)", stripped):
        return True
    return bool(re.match(r"^[A-Za-z_][\w.$]*(?:Exception|Error):\s*", stripped))


def _is_error_event_continuation(line):
    """判断 _is_error_event_continuation 对应的业务数据，保持现有调用约定。"""
    text = line or ""
    stripped = text.strip()
    if not stripped:
        return True
    if re.match(r"^(at\s+[\w.$]+\(|Caused by:|\.\.\.\s+\d+\s+more|File\s+\")", stripped):
        return True
    if re.match(r"^[A-Za-z_][\w.$]*(?:Exception|Error):\s*", stripped):
        return True
    if re.match(r"^(message|reason|error|desc|url|queryString)\s*[:=]", stripped, re.IGNORECASE):
        return True
    if re.search(r"\b(rpc error|connection closed|timeout|reset by peer|refused|unavailable|nullpointer)\b", stripped, re.IGNORECASE):
        return True
    return False


def _collect_log_error_event(lines, start, max_event_lines):
    """处理 _collect_log_error_event 对应的业务步骤，并向调用方返回所需结果。"""
    event_lines = [lines[start]]
    cursor = start + 1
    while cursor < len(lines) and len(event_lines) < max_event_lines:
        next_line = lines[cursor]
        if _is_log_record_start(next_line):
            break
        if _is_error_event_continuation(next_line) or event_lines:
            event_lines.append(next_line)
            cursor += 1
            continue
        break
    return {
        "line": start + 1,
        "end_line": cursor,
        "text": "\n".join(event_lines).strip(),
    }


def _is_success_recorded_as_error(event_text):
    """判断 _is_success_recorded_as_error 对应的业务数据，保持现有调用约定。"""
    text = str(event_text or "")
    if re.search(r"(?:Exception|Error):|Traceback \(most recent call last\)|panic:\s+", text):
        return False
    return bool(re.search(
        r"(?:打标结果为|(?:tagging\s+)?result\s*(?:is|=|:))[^\n]{0,80}message\s*[:=]\s*[\"']?success\b",
        text,
        re.IGNORECASE,
    ))


def extract_log_error_events(log_content, max_events=None, max_event_lines=80):
    """解析或提取并返回 extract_log_error_events 对应的业务数据，保持现有调用约定。"""
    text = str(log_content or "")
    if not text:
        return []

    lines = text.splitlines()
    events = []
    consumed_until = -1
    for index, line in enumerate(lines):
        if max_events is not None and len(events) >= max_events:
            break
        if index <= consumed_until:
            continue
        if not _is_error_event_start(line):
            continue

        event = _collect_log_error_event(lines, index, max_event_lines)
        if not event["text"]:
            continue
        if _is_success_recorded_as_error(event["text"]):
            consumed_until = max(consumed_until, event["end_line"] - 1)
            continue
        events.append({
            "line": event["line"],
            "text": event["text"],
        })
        consumed_until = max(consumed_until, event["end_line"] - 1)
        if max_events is not None and len(events) >= max_events:
            break

    return events


def _normalize_log_event_signature_text(value):
    """规范化并返回 _normalize_log_event_signature_text 对应的业务数据，保持现有调用约定。"""
    text = str(value or "").lower()
    text = re.sub(r"^\s*\d{4}-\d{2}-\d{2}[ t]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?", "", text)
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<uuid>", text)
    text = re.sub(r"0x[0-9a-f]+", "<address>", text)
    text = re.sub(r"\b\d+\b", "<n>", text)
    return re.sub(r"\s+", " ", text).strip()


def build_log_event_signature(event_text):
    """构建并返回 build_log_event_signature 对应的业务数据，保持现有调用约定。"""
    text = str(event_text or "")
    exception_matches = re.findall(
        r"(?m)^\s*([A-Za-z_][\w.$]*(?:Exception|Error)):\s*(.*)$",
        text,
    )
    panic_match = re.search(r"(?mi)^\s*(panic):\s*(.*)$", text)
    if exception_matches:
        error_type, message = exception_matches[0]
    elif panic_match:
        error_type, message = panic_match.group(1), panic_match.group(2)
    else:
        first_line = next((line for line in text.splitlines() if line.strip()), "unknown error")
        error_type, message = "log_error", first_line

    locations = extract_source_locations(text)
    first_location = locations[0] if locations else {}
    location_key = ":".join(
        str(value or "")
        for value in [
            first_location.get("language"),
            first_location.get("file"),
            first_location.get("line"),
            first_location.get("symbol"),
        ]
    )
    return "|".join([
        _normalize_log_event_signature_text(error_type),
        _normalize_log_event_signature_text(message),
        _normalize_log_event_signature_text(location_key),
    ])


def group_log_error_events(events):
    """合并整理并返回 group_log_error_events 对应的业务数据，保持现有调用约定。"""
    groups = []
    groups_by_signature = {}
    for event in events or []:
        event_text = str(event.get("text") or "")
        signature = build_log_event_signature(event_text)
        group = groups_by_signature.get(signature)
        if group is None:
            group = {
                "signature": signature,
                "first_line": event.get("line"),
                "last_line": event.get("line"),
                "occurrence_count": 0,
                "occurrence_lines": [],
                "representative_text": event_text,
                "languages": detect_log_languages(event_text),
            }
            groups_by_signature[signature] = group
            groups.append(group)
        group["occurrence_count"] += 1
        group["last_line"] = event.get("line")
        if event.get("line") is not None:
            group["occurrence_lines"].append(event.get("line"))
    return groups


def _truncate_group_evidence(text, max_chars):
    """处理 _truncate_group_evidence 对应的业务步骤，并向调用方返回所需结果。"""
    value = str(text or "")
    if len(value) <= max_chars:
        return value
    marker = "\n... group context truncated ...\n"
    available = max(40, max_chars - len(marker))
    head_chars = available * 2 // 3
    tail_chars = available - head_chars
    return f"{value[:head_chars]}{marker}{value[-tail_chars:]}"


def build_backend_log_analysis(log_content, max_chars=None):
    """构建并返回 build_backend_log_analysis 对应的业务数据，保持现有调用约定。"""
    _, default_max_chars = get_error_context_settings()
    max_chars = max(800, int(max_chars or default_max_chars))
    groups = group_log_error_events(extract_log_error_events(log_content))
    if not groups:
        return "grouped_issue_count: 0\nNo explicit ERROR/Exception/FATAL/panic event was detected."

    header = (
        f"grouped_issue_count: {len(groups)}\n"
        "The backend scanned the complete input and grouped repeated events. "
        "Every group below must be represented in the final issues array.\n"
    )
    available = max(200, max_chars - len(header))
    per_group_budget = max(180, available // len(groups))
    rendered_groups = []
    for index, group in enumerate(groups, start=1):
        metadata = (
            f"\n[issue_group_{index}]\n"
            f"first_line: {group.get('first_line')}\n"
            f"last_line: {group.get('last_line')}\n"
            f"occurrence_count: {group.get('occurrence_count')}\n"
            f"languages: {', '.join(group.get('languages') or []) or 'unknown'}\n"
            "representative_event:\n"
        )
        evidence_budget = max(80, per_group_budget - len(metadata))
        rendered_groups.append(
            metadata + _truncate_group_evidence(group.get("representative_text"), evidence_budget)
        )
    return (header + "".join(rendered_groups))[:max_chars]


def build_log_error_event_summary(log_content, max_events=None):
    """构建并返回 build_log_error_event_summary 对应的业务数据，保持现有调用约定。"""
    events = extract_log_error_events(log_content, max_events=max_events)
    if not events:
        return "未识别到明确的 ERROR/Exception/FATAL/panic 事件。"

    total = len(events)
    lines = [f"共识别到 {total} 段错误/异常事件，以下按日志出现顺序列出，分析时必须逐段覆盖："]
    for index, event in enumerate(events, start=1):
        lines.append(f"\n[{index}] line {event['line']}")
        lines.append(event["text"])
    return "\n".join(lines)


def build_full_log_analysis_context(log_content, max_chars=None):
    """构建并返回 build_full_log_analysis_context 对应的业务数据，保持现有调用约定。"""
    compact_context = build_compact_log_context(log_content, max_chars=max_chars)
    error_summary = build_backend_log_analysis(log_content, max_chars=max_chars)
    return (
        f"{compact_context}\n\n"
        "【全量错误清单】\n"
        f"{error_summary}"
    )


def build_log_analysis_prompt(log_content):
    """构建并返回 build_log_analysis_prompt 对应的业务数据，保持现有调用约定。"""
    context_lines, max_chars = get_error_context_settings()
    full_context = build_full_log_analysis_context(log_content, max_chars=max_chars)
    return (
        "\u8bf7\u57fa\u4e8e\u3010\u65e5\u5fd7\u9519\u8bef\u5173\u952e\u6bb5\u53ca\u4e0a\u4e0b\u6587\u3011\u505a\u521d\u6b65\u5206\u6790\uff0c\u4e0d\u8981\u53ea\u6839\u636e ERROR \u5355\u884c\u4e0b\u7ed3\u8bba\u3002\n"
        f"\u540e\u7aef\u5df2\u4ece\u5b8c\u6574\u65e5\u5fd7\u4e0a\u4e0b\u6587\u4e2d\u63d0\u53d6\u9519\u8bef/\u5f02\u5e38\u5173\u952e\u884c\u53ca\u524d\u540e\u7ea6 {context_lines} \u884c\u4e0a\u4e0b\u6587\uff0c\u4ee5\u4fbf\u7ed3\u5408\u65f6\u5e8f\u548c\u8bf7\u6c42\u94fe\u8def\u5224\u65ad\u3002\n"
        "必须逐条覆盖【全量错误清单】中的每一段错误/异常事件；如果多段错误属于同一根因，可以合并说明，但不能遗漏。\n"
        "\u5206\u6790\u65f6\u9700\u8981\u540c\u65f6\u5173\u6ce8 INFO\u3001WARN\u3001DEBUG\u3001ERROR \u4ee5\u53ca\u9519\u8bef\u524d\u540e\u7684\u8bf7\u6c42\u94fe\u8def\u3001\u72b6\u6001\u53d8\u5316\u3001\u8017\u65f6\u3001\u91cd\u8bd5\u3001\u8fde\u63a5\u3001\u914d\u7f6e\u52a0\u8f7d\u7b49\u4e0a\u4e0b\u6587\u3002\n"
        "\u8bf7\u5224\u65ad\u95ee\u9898\u53ef\u80fd\u6765\u81ea\u4ee3\u7801\u3001\u914d\u7f6e\u3001\u7f51\u7edc\u3001\u6570\u636e\u3001\u4f9d\u8d56/\u7b2c\u4e09\u65b9\u670d\u52a1\u3001\u8d44\u6e90\u6216\u672a\u77e5\u3002\n\n"
        f"{full_context}"
    )


def build_compact_log_context(log_content, max_chars=None):
    """构建并返回 build_compact_log_context 对应的业务数据，保持现有调用约定。"""
    context_lines, default_max_chars = get_error_context_settings()
    max_chars = max_chars or default_max_chars
    relevant_context = extract_relevant_log_context(log_content, context_lines, max_chars)
    return (
        "\u3010\u65e5\u5fd7\u9519\u8bef\u5173\u952e\u6bb5\u53ca\u4e0a\u4e0b\u6587\u3011\n"
        "\u4ee5\u4e0b\u4e3a\u4ece\u5b8c\u6574\u65e5\u5fd7\u4e2d\u63d0\u53d6\u7684\u9519\u8bef/\u5f02\u5e38\u5173\u952e\u6bb5\u548c\u524d\u540e\u4e0a\u4e0b\u6587\uff0c\u7528\u4e8e\u7ed3\u5408\u4ee3\u7801\u5feb\u901f\u5b9a\u4f4d\u3002\n"
        f"{relevant_context}"
    )

def analyze_log_with_deepseek(log_content):
    """执行故障分析并返回 analyze_log_with_deepseek 对应的业务数据，保持现有调用约定。"""
    logging.info("==> \u8fdb\u5165 analyze_log_with_AI")
    try:
        logging.info("\u8c03\u7528 AI API \u8fdb\u884c\u65e5\u5fd7\u9519\u8bef\u5173\u952e\u6bb5\u5206\u6790")
        system_prompt = "\u4f60\u662f\u4e00\u4e2a\u65e5\u5fd7\u5206\u6790\u4e13\u5bb6\uff0c\u64c5\u957f\u7ed3\u5408\u5b8c\u6574\u4e0a\u4e0b\u6587\u8bc6\u522b\u6545\u969c\u94fe\u8def\uff0c\u800c\u4e0d\u662f\u53ea\u770b\u5355\u4e2a\u9519\u8bef\u884c\u3002"
        result = call_ai_model(system_prompt, build_log_analysis_prompt(log_content))
        logging.info("AI \u65e5\u5fd7\u9519\u8bef\u5173\u952e\u6bb5\u5206\u6790\u6210\u529f")
        return result
    except AiServiceError:
        raise
    except Exception as e:
        logging.exception(f"\u8c03\u7528 AI \u65e5\u5fd7\u5206\u6790\u5931\u8d25: {e}")
        return None


def guess_image_mime_type(image_path):
    """查找或推断并返回 guess_image_mime_type 对应的业务数据，保持现有调用约定。"""
    suffix = os.path.splitext(str(image_path or ""))[1].lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }.get(suffix, "image/png")


def build_image_data_url(image_path):
    """构建并返回 build_image_data_url 对应的业务数据，保持现有调用约定。"""
    with open(image_path, "rb") as file_obj:
        encoded = base64.b64encode(file_obj.read()).decode("ascii")
    return f"data:{guess_image_mime_type(image_path)};base64,{encoded}"


def call_ai_multimodal_model(system_prompt, user_prompt, image_data_url):
    """处理 call_ai_multimodal_model 对应的业务步骤，并向调用方返回所需结果。"""
    api_key = app.config.get("OPENAI_KEY")
    base_url = app.config.get("OPENAI_URL")
    api_style = str(app.config.get("OPENAI_API_STYLE", "chat") or "chat").lower()
    request_options = build_ai_request_options(app.config, api_style)
    model = request_options["model"]
    if not api_key or not base_url:
        raise ValueError("API Key \u6216 Base URL \u672a\u914d\u7f6e")

    image_data_urls = image_data_url if isinstance(image_data_url, list) else [image_data_url]
    image_data_urls = [item for item in image_data_urls if item]
    logging.info("\u5f00\u59cb\u8c03\u7528\u591a\u6a21\u6001 AI\uff1amodel=%s, style=%s", model, api_style)
    client = OpenAI(api_key=api_key, base_url=base_url)
    try:
        if api_style in ("response", "responses"):
            content = [{"type": "input_text", "text": user_prompt}]
            content.extend({"type": "input_image", "image_url": image_url} for image_url in image_data_urls)
            response = client.responses.create(
                **request_options,
                input=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": content,
                    },
                ],
            )
            return extract_responses_text(response)

        content = [{"type": "text", "text": user_prompt}]
        content.extend({"type": "image_url", "image_url": {"url": image_url}} for image_url in image_data_urls)
        response = client.chat.completions.create(
            **request_options,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": content,
                },
            ],
            stream=False,
        )
        return response.choices[0].message.content
    except AiServiceError:
        raise
    except Exception as exc:
        raise normalize_ai_exception(exc) from exc


def normalize_image_analysis(image_tag, payload, image_description=""):
    """规范化并返回 normalize_image_analysis 对应的业务数据，保持现有调用约定。"""
    data = payload if isinstance(payload, dict) else {}
    text_candidates = [
        data.get("extracted_text"),
        data.get("ocr_text"),
        data.get("text"),
    ]
    extracted_text = next((str(item).strip() for item in text_candidates if str(item or "").strip()), "")
    summary = str(data.get("summary") or data.get("screen_summary") or data.get("error_summary") or "").strip()
    keywords = data.get("keywords") if isinstance(data.get("keywords"), list) else []
    components = data.get("components") if isinstance(data.get("components"), list) else []
    business_context = data.get("business_context") if isinstance(data.get("business_context"), dict) else {}
    missing_context = data.get("missing_context") if isinstance(data.get("missing_context"), list) else []
    scene_summary = str(data.get("scene_summary") or business_context.get("scene_summary") or "").strip()
    business_domain = str(data.get("business_domain") or business_context.get("business_domain") or "").strip()
    page_name = str(data.get("page_name") or business_context.get("page_name") or "").strip()
    user_action = str(data.get("user_action") or business_context.get("user_action") or "").strip()
    error_message = str(data.get("error_message") or business_context.get("error_message") or "").strip()
    visible_fields = data.get("visible_fields") if isinstance(data.get("visible_fields"), list) else []
    candidate_apis = data.get("candidate_apis") if isinstance(data.get("candidate_apis"), list) else []
    candidate_code_keywords = (
        data.get("candidate_code_keywords") if isinstance(data.get("candidate_code_keywords"), list) else []
    )
    if image_tag == "business_image":
        keywords = [
            *keywords,
            business_domain,
            page_name,
            user_action,
            error_message,
            *visible_fields,
            *candidate_apis,
            *candidate_code_keywords,
        ]
    analysis_text = build_image_analysis_context(
        {
            "image_tag": image_tag,
            "summary": summary,
            "extracted_text": extracted_text,
            "keywords": keywords,
            "components": components,
            "scene_summary": scene_summary,
            "business_domain": business_domain,
            "page_name": page_name,
            "user_action": user_action,
            "error_message": error_message,
            "visible_fields": visible_fields,
            "candidate_apis": candidate_apis,
            "candidate_code_keywords": candidate_code_keywords,
            "missing_context": missing_context,
        },
        image_description,
    )
    detected_components = detect_error_components(
        "\n".join([summary, extracted_text, " ".join(str(item) for item in keywords), image_description])
    )
    merged_components = []
    for item in [*components, *detected_components]:
        normalized = str(item or "").strip().lower()
        if normalized and normalized not in merged_components:
            merged_components.append(normalized)
    return {
        "image_tag": image_tag,
        "image_description": image_description,
        "summary": summary,
        "extracted_text": extracted_text,
        "keywords": [str(item).strip() for item in keywords if str(item).strip()],
        "components": merged_components,
        "scene_summary": scene_summary,
        "business_domain": business_domain,
        "page_name": page_name,
        "user_action": user_action,
        "error_message": error_message,
        "visible_fields": [str(item).strip() for item in visible_fields if str(item).strip()],
        "candidate_apis": [str(item).strip() for item in candidate_apis if str(item).strip()],
        "candidate_code_keywords": [str(item).strip() for item in candidate_code_keywords if str(item).strip()],
        "missing_context": missing_context,
        "analysis_text": analysis_text,
    }


def analyze_uploaded_image(image_path, image_tag, image_description=""):
    """执行故障分析并返回 analyze_uploaded_image 对应的业务数据，保持现有调用约定。"""
    image_paths = image_path if isinstance(image_path, list) else [image_path]
    image_paths = [path for path in image_paths if path]
    image_data_url = [build_image_data_url(path) for path in image_paths]
    image_type_label = "\u65e5\u5fd7\u622a\u56fe" if image_tag == "log_image" else "\u4e1a\u52a1\u622a\u56fe"
    multi_image_schema = ""
    if len(image_paths) > 1:
        image_labels = "、".join(f"第{index}张" for index in range(1, len(image_paths) + 1))
        multi_image_schema = (
            f"本次共上传 {len(image_paths)} 张图片（{image_labels}），必须按上传顺序逐张输出识别结果，"
            "不能只分析第一张。JSON 必须额外包含 image_findings 数组，每张图片对应一个对象，"
            "字段包含 image_index、summary、extracted_text、visible_errors。每张图片至少保留一条记录；"
            "只有确认是同一错误的重复截图时才允许合并，并在 summary 中说明合并依据。\n"
        )
    business_schema = ""
    if image_tag == "business_image":
        business_schema = (
            "业务截图必须额外识别业务场景，并返回字段：scene_summary、business_domain、page_name、"
            "user_action、error_message、visible_fields、candidate_apis、candidate_code_keywords、missing_context。\n"
            "missing_context 用于提醒用户补充上下游信息；如果截图无法确认请求参数、业务ID、服务日志、"
            "DB/Redis/ES/Kafka/RPC 下游返回，就必须列出 direction(upstream/downstream/current)、"
            "title、reason、needed、how_to_get。\n"
            "多张业务图片可以关联为同一流程，但仍必须先逐张识别，再判断是否属于同一问题。\n"
        )
    image_description_text = image_description or "\u65e0"
    prompt = (
        f"\u8bf7\u8bc6\u522b\u8fd9\u5f20{image_type_label}\uff0c\u5e76\u4e14\u53ea\u8fd4\u56de JSON\u3002\n"
        "JSON \u5fc5\u987b\u5305\u542b\uff1asummary\u3001extracted_text\u3001keywords\u3001components\u3002\n"
        "\u5982\u679c\u662f\u65e5\u5fd7\u622a\u56fe\uff0c\u8bf7\u5c3d\u91cf\u63d0\u53d6\u9519\u8bef\u65e5\u5fd7\u3001\u5f02\u5e38\u5806\u6808\u3001\u9519\u8bef\u5173\u952e\u5b57\u3002\n"
        "\u5982\u679c\u662f\u4e1a\u52a1\u622a\u56fe\uff0c\u8bf7\u63d0\u53d6\u9875\u9762\u63d0\u793a\u3001\u5173\u952e\u4e1a\u52a1\u72b6\u6001\u3001\u62a5\u9519\u6587\u6848\uff0c\u4ee5\u53ca\u53ef\u7528\u4e8e\u4ee3\u7801\u6392\u67e5\u7684\u5173\u952e\u5b57\u3002\n"
        f"{multi_image_schema}"
        f"{business_schema}"
        f"\u7528\u6237\u8865\u5145\u63cf\u8ff0\uff1a{image_description_text}"
    )
    logging.info("\u5f00\u59cb\u8bc6\u522b\u4e0a\u4f20\u56fe\u7247\uff1apath=%s, image_tag=%s", image_paths, image_tag)
    result = call_ai_multimodal_model("\u4f60\u662f\u4e00\u4e2a\u56fe\u7247\u6545\u969c\u8bc6\u522b\u4e13\u5bb6\u3002", prompt, image_data_url)
    payload = extract_json_object(result)
    logging.info("\u56fe\u7247\u8bc6\u522b\u5b8c\u6210\uff1apayload_keys=%s", list(payload.keys()) if isinstance(payload, dict) else [])
    return normalize_image_analysis(image_tag, payload, image_description=image_description)


def build_image_analysis_context(image_analysis, image_description=""):
    """构建并返回 build_image_analysis_context 对应的业务数据，保持现有调用约定。"""
    image_tag = str((image_analysis or {}).get("image_tag") or "").strip().lower()
    image_type_label = "\u65e5\u5fd7\u622a\u56fe" if image_tag == "log_image" else "\u4e1a\u52a1\u622a\u56fe"
    summary = str((image_analysis or {}).get("summary") or "").strip()
    extracted_text = str((image_analysis or {}).get("extracted_text") or "").strip()
    keywords = image_analysis.get("keywords") or []
    components = image_analysis.get("components") or []
    business_fields = [
        ("业务场景", image_analysis.get("scene_summary")),
        ("业务域", image_analysis.get("business_domain")),
        ("页面名称", image_analysis.get("page_name")),
        ("用户动作", image_analysis.get("user_action")),
        ("页面报错", image_analysis.get("error_message")),
    ]
    lines = [f"\u56fe\u7247\u7c7b\u578b: {image_type_label}"]
    if image_description:
        lines.append(f"\u7528\u6237\u63cf\u8ff0: {image_description}")
    for label, value in business_fields:
        value = str(value or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    if summary:
        lines.append(f"\u56fe\u7247\u6458\u8981: {summary}")
    if extracted_text:
        lines.append(f"\u8bc6\u522b\u6587\u672c: {extracted_text}")
    if image_analysis.get("visible_fields"):
        lines.append("页面字段: " + ", ".join(str(item) for item in image_analysis.get("visible_fields") or []))
    if image_analysis.get("candidate_apis"):
        lines.append("候选接口: " + ", ".join(str(item) for item in image_analysis.get("candidate_apis") or []))
    if image_analysis.get("candidate_code_keywords"):
        lines.append("候选代码关键词: " + ", ".join(str(item) for item in image_analysis.get("candidate_code_keywords") or []))
    if keywords:
        lines.append("\u5173\u952e\u8bcd: " + ", ".join(str(item) for item in keywords))
    if components:
        lines.append("\u7591\u4f3c\u7ec4\u4ef6: " + ", ".join(str(item) for item in components))
    missing_context = image_analysis.get("missing_context") or []
    if missing_context:
        lines.append("需要补充的信息:")
        for item in missing_context:
            if not isinstance(item, dict):
                continue
            direction = item.get("direction") or "unknown"
            title = item.get("title") or "未命名信息缺口"
            needed = item.get("needed") or []
            how_to_get = item.get("how_to_get") or []
            lines.append(f"- {direction}: {title}")
            if item.get("reason"):
                lines.append(f"  原因: {item.get('reason')}")
            if needed:
                lines.append("  需要: " + ", ".join(str(value) for value in needed))
            if how_to_get:
                lines.append("  获取方式: " + ", ".join(str(value) for value in how_to_get))
    return "\n".join(lines)


def call_ai_model(system_prompt, user_prompt):
    """处理 call_ai_model 对应的业务步骤，并向调用方返回所需结果。"""
    api_key = app.config.get("OPENAI_KEY")
    base_url = app.config.get("OPENAI_URL")
    api_style = app.config.get("OPENAI_API_STYLE", "chat")
    api_style = str(api_style or "chat").lower()
    request_options = build_ai_request_options(app.config, api_style)
    model = request_options["model"]
    logging.info(
        "AI \u914d\u7f6e\u68c0\u67e5 - API_KEY: %s, BASE_URL: %s, MODEL: %s, STYLE: %s",
        "\u5b58\u5728" if api_key else "\u7f3a\u5931",
        base_url,
        model,
        api_style,
    )

    if not api_key or not base_url:
        raise ValueError("API Key \u6216 Base URL \u672a\u914d\u7f6e")

    client = OpenAI(api_key=api_key, base_url=base_url)
    try:
        if api_style in ("response", "responses"):
            response = client.responses.create(
                **request_options,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            return extract_responses_text(response)

        response = client.chat.completions.create(
            **request_options,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            stream=False,
        )
        return response.choices[0].message.content
    except AiServiceError:
        raise
    except Exception as exc:
        raise normalize_ai_exception(exc) from exc


def extract_responses_text(response):
    """解析或提取并返回 extract_responses_text 对应的业务数据，保持现有调用约定。"""
    output_text = getattr(response, "output_text", None)
    if output_text:
        return output_text

    if isinstance(response, dict):
        output_text = response.get("output_text")
        if output_text:
            return output_text
        output = response.get("output", [])
    else:
        output = getattr(response, "output", [])

    text_parts = []
    for item in output or []:
        content = item.get("content", []) if isinstance(item, dict) else getattr(item, "content", [])
        for block in content or []:
            if isinstance(block, dict):
                text = block.get("text")
            else:
                text = getattr(block, "text", None)
            if text:
                text_parts.append(text)
    return "\n".join(text_parts).strip()

def extract_error_info_from_log(log_content):
    """解析或提取并返回 extract_error_info_from_log 对应的业务数据，保持现有调用约定。"""
    logging.info("==> \u8fdb\u5165 extract_error_info_from_log")
    source_locations = extract_source_locations(log_content)
    if source_locations:
        logging.info(
            "\u63d0\u53d6\u5230\u591a\u8bed\u8a00\u5806\u6808\u4fe1\u606f\uff1acount=%s, languages=%s",
            len(source_locations),
            detect_log_languages(log_content),
        )
        return source_locations

    error_info = []
    error_matches = re.findall(
        r'(?:^|\n)\s*([A-Za-z_][\w\.]*(?:Exception|Error):\s*.*)',
        log_content,
    )
    if error_matches:
        error_message = error_matches[-1].strip()
    else:
        error_line = re.search(r'(Exception|Error):\s*(.*)', log_content)
        panic_line = re.search(r'(?m)^\s*(panic:\s*.+)$', log_content)
        error_message = (
            error_line.group(0).strip()
            if error_line
            else panic_line.group(1).strip()
            if panic_line
            else "\u672a\u77e5\u9519\u8bef"
        )
    logging.info("\u5339\u914d\u5230\u7684\u9519\u8bef\u63cf\u8ff0\uff1a%s", error_message)

    python_frames = re.findall(
        r'File\s+"([^"]+)",\s+line\s+(\d+)(?:,\s+in\s+[^\n]+)?',
        log_content,
    )
    if python_frames:
        for file_name, line_str in python_frames:
            error_info.append({
                "file": os.path.basename(file_name),
                "line": int(line_str),
                "error": error_message,
            })
        logging.info("\u63d0\u53d6\u5230 Python \u5806\u6808\u4fe1\u606f\uff1acount=%s", len(error_info))
        return error_info

    go_frames = re.findall(
        r'(?m)^\s+([^\s]+\.go):(\d+)(?:\s+\+0x[0-9a-fA-F]+)?',
        log_content,
    )
    if go_frames:
        for file_name, line_str in go_frames:
            error_info.append({
                "file": os.path.basename(file_name),
                "line": int(line_str),
                "error": error_message,
            })
        logging.info("\u63d0\u53d6\u5230 Go \u5806\u6808\u4fe1\u606f\uff1acount=%s", len(error_info))
        return error_info

    stack_matches = re.findall(r'\s+at\s+[\w.$]+\(([\w.$-]+):(\d+)\)', log_content)
    if stack_matches:
        for file_name, line_str in stack_matches:
            try:
                line_number = int(line_str)
            except ValueError:
                logging.warning("\u5806\u6808\u884c\u53f7\u8f6c\u6362\u5931\u8d25\uff0c\u9ed8\u8ba4\u4f7f\u7528 1")
                line_number = 1
            logging.info("\u63d0\u53d6\u5230\u5806\u6808\u4fe1\u606f\uff1a\u6587\u4ef6 %s, \u884c\u53f7 %s", file_name, line_number)
            error_info.append({
                "file": file_name,
                "line": line_number,
                "error": error_message
            })
    else:
        logging.info("\u672a\u5339\u914d\u5230\u5806\u6808\u4fe1\u606f\uff0c\u4ec5\u4fdd\u7559\u9519\u8bef\u63cf\u8ff0")
        error_info.append({
            "file": None,
            "line": None,
            "error": error_message
        })
    return error_info


def resolve_file_paths(repo_path, error_info):
    """解析并返回 resolve_file_paths 对应的业务数据，保持现有调用约定。"""
    logging.info("==> \u8fdb\u5165 resolve_file_paths")
    resolved = []
    for err in error_info:
        target = err["file"]
        found_path = None
        logging.info("\u5f00\u59cb\u5728\u4ed3\u5e93\u4e2d\u67e5\u627e\u76ee\u6807\u6587\u4ef6\uff1arepo=%s, target=%s", repo_path, target)
        for root, _, files in os.walk(repo_path):
            if target in files:
                found_path = os.path.join(root, target)
                logging.info("\u627e\u5230\u76ee\u6807\u6587\u4ef6\uff1a%s", found_path)
                break
        if found_path:
            resolved.append({
                "file": found_path,
                "line": err["line"],
                "error": err["error"]
            })
        else:
            logging.warning("\u76ee\u6807\u6587\u4ef6\u672a\u5728\u4ed3\u5e93\u4e2d\u627e\u5230\uff1arepo=%s, target=%s", repo_path, target)
    return resolved


def extract_code_snippets(resolved_errors):
    """解析或提取并返回 extract_code_snippets 对应的业务数据，保持现有调用约定。"""
    logging.info("==> \u8fdb\u5165 extract_code_snippets")
    code_snippets = []
    for err in resolved_errors:
        file_path = err.get("file")
        line_number = err.get("line")
        logging.info("\u51c6\u5907\u63d0\u53d6\u4ee3\u7801\u7247\u6bb5\uff1afile=%s, line=%s", file_path, line_number)
        if not file_path or not os.path.exists(file_path):
            logging.warning("\u4ee3\u7801\u6587\u4ef6\u4e0d\u5b58\u5728\uff0c\u8df3\u8fc7\u63d0\u53d6\uff1a%s", file_path)
            continue
        try:
            with open(file_path, "r", encoding="utf-8") as file_obj:
                lines = file_obj.readlines()
            start_line = max(1, line_number - 5)
            end_line = min(len(lines), line_number + 4)
            snippet_lines = lines[start_line - 1:end_line]
            snippet = "".join(snippet_lines)
            numbered_snippet = format_numbered_snippet(snippet_lines, start_line, line_number)
            try:
                display_file = os.path.relpath(file_path)
            except ValueError:
                display_file = file_path
            logging.info(
                "\u4ee3\u7801\u7247\u6bb5\u63d0\u53d6\u6210\u529f\uff1afile=%s, target_line=%s, range=%s-%s",
                file_path,
                line_number,
                start_line,
                end_line,
            )
            code_snippets.append({
                "file": display_file,
                "line": line_number,
                "start_line": start_line,
                "end_line": end_line,
                "snippet": snippet,
                "numbered_snippet": numbered_snippet,
                "reason": err.get("reason"),
                "component": err.get("component"),
            })
        except Exception as exc:
            logging.warning("\u8bfb\u53d6\u4ee3\u7801\u6587\u4ef6\u5931\u8d25\uff1afile=%s, error=%s", file_path, exc)
    return code_snippets


def detect_error_components(log_content):
    """处理 detect_error_components 对应的业务步骤，并向调用方返回所需结果。"""
    text = str(log_content or "").lower()
    components = []
    for component, patterns in COMPONENT_USAGE_PATTERNS.items():
        if any(pattern in text for pattern in patterns):
            components.append(component)
    return components


def find_component_code_usages(repo_path, components, max_matches=8):
    """查找或推断并返回 find_component_code_usages 对应的业务数据，保持现有调用约定。"""
    if not repo_path or not os.path.exists(repo_path) or not components:
        return []

    matches = []
    seen = set()
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [name for name in dirs if name not in SKIP_CODE_SEARCH_DIRS]
        for filename in files:
            suffix = os.path.splitext(filename)[1].lower()
            if suffix not in SOURCE_CODE_SUFFIXES:
                continue

            file_path = os.path.join(root, filename)
            lines = read_text_lines(file_path)
            if not lines:
                continue

            for line_index, line in enumerate(lines, start=1):
                lowered_line = line.lower()
                for component in components:
                    patterns = COMPONENT_USAGE_PATTERNS.get(component, [])
                    if not any(pattern in lowered_line for pattern in patterns):
                        continue

                    key = (file_path, line_index, component)
                    if key in seen:
                        continue
                    seen.add(key)
                    matches.append({
                        "file": file_path,
                        "line": line_index,
                        "error": f"{component} \u8c03\u7528\u4f4d\u7f6e",
                        "component": component,
                        "reason": f"\u65e5\u5fd7\u4e2d\u51fa\u73b0 {component.upper()} \u76f8\u5173\u62a5\u9519\uff0c\u5df2\u5728\u4ee3\u7801\u4ed3\u5e93\u4e2d\u5b9a\u4f4d\u5230\u8be5\u7ec4\u4ef6\u7684\u8c03\u7528\u4f4d\u7f6e\u3002",
                    })
                    if len(matches) >= max_matches:
                        return extract_code_snippets(matches)
    return extract_code_snippets(matches)


def read_text_lines(file_path):
    """读取并返回 read_text_lines 对应的业务数据，保持现有调用约定。"""
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            with open(file_path, "r", encoding=encoding) as file_obj:
                return file_obj.readlines()
        except UnicodeDecodeError:
            continue
        except OSError:
            return []
    return []


def normalize_module_name(value):
    """规范化并返回 normalize_module_name 对应的业务数据，保持现有调用约定。"""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def normalize_service_module_name(value):
    """规范化并返回 normalize_service_module_name 对应的业务数据，保持现有调用约定。"""
    name = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").lower()).strip("-")
    name = re.sub(r"-svc$", "", name)
    return name


INFRASTRUCTURE_MODULE_NAME_PATTERNS = [
    re.compile(r"(^|-)minio($|-)"),
    re.compile(r"(^|-)cluster($|-)"),
    re.compile(r"^zhuiyi-[a-z0-9-]+$"),
]


def is_infrastructure_or_bucket_name(value):
    """判断 is_infrastructure_or_bucket_name 对应的业务数据，保持现有调用约定。"""
    name = normalize_service_module_name(value)
    if not name:
        return True
    return any(pattern.search(name) for pattern in INFRASTRUCTURE_MODULE_NAME_PATTERNS)


def module_name_matches_text(module_name, source_text):
    """处理 module_name_matches_text 对应的业务步骤，并向调用方返回所需结果。"""
    normalized = normalize_service_module_name(module_name)
    if not normalized:
        return False
    parts = [part for part in re.split(r"[-_]+", normalized) if part]
    if not parts:
        return False
    separator = r"[-_\.]?"
    core_pattern = separator.join(re.escape(part) for part in parts)
    pattern = rf"(?<![a-z0-9]){core_pattern}(?:-svc)?(?![a-z0-9])"
    return re.search(pattern, str(source_text or "").lower()) is not None


def iter_source_files(repo_path):
    """处理 iter_source_files 对应的业务步骤，并向调用方返回所需结果。"""
    if not repo_path or not os.path.exists(repo_path):
        return
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [name for name in dirs if name not in SKIP_CODE_SEARCH_DIRS]
        for filename in files:
            suffix = os.path.splitext(filename)[1].lower()
            if suffix in SOURCE_CODE_SUFFIXES:
                yield os.path.join(root, filename)


def discover_related_modules(repo_path, log_content, available_modules, primary_module_id=None, max_modules=8):
    """查找或推断并返回 discover_related_modules 对应的业务数据，保持现有调用约定。"""
    primary_id = str(primary_module_id or "")
    candidates = []
    for module in available_modules or []:
        module_id = str(module.get("module_id") or module.get("moduleId") or "")
        module_name = module.get("module_name") or module.get("moduleName") or module.get("name")
        normalized = normalize_module_name(module_name)
        if not module_name or not normalized or module_id == primary_id:
            continue
        candidates.append({
            "moduleId": module_id or None,
            "moduleName": str(module_name),
            "normalized": normalized,
        })

    found = {}
    log_text = normalize_module_name(log_content)
    for candidate in candidates:
        if candidate["normalized"] in log_text:
            found[candidate["moduleName"]] = {
                "moduleId": candidate["moduleId"],
                "moduleName": candidate["moduleName"],
                "role": "related",
                "reason": "日志中出现该模块名称，可能位于本次故障链路上。",
            }

    for file_path in iter_source_files(repo_path):
        lines = read_text_lines(file_path)
        if not lines:
            continue
        text = normalize_module_name("".join(lines))
        for candidate in candidates:
            if candidate["moduleName"] in found:
                continue
            if candidate["normalized"] in text:
                found[candidate["moduleName"]] = {
                    "moduleId": candidate["moduleId"],
                    "moduleName": candidate["moduleName"],
                    "role": "related",
                    "reason": f"主模块代码中出现该模块名称：{os.path.basename(file_path)}。",
                }
                if len(found) >= max_modules:
                    return list(found.values())
    return list(found.values())[:max_modules]


def infer_related_module_role(source_text, module_name):
    """查找或推断并返回 infer_related_module_role 对应的业务数据，保持现有调用约定。"""
    normalized_text = normalize_module_name(source_text)
    normalized_name = normalize_module_name(module_name)
    index = normalized_text.find(normalized_name)
    if index < 0:
        return "related"
    window = normalized_text[max(0, index - 120):index + len(normalized_name) + 120]
    if any(token in window for token in ("callback", "webhook", "notify", "return", "response")):
        return "upstream"
    if any(token in window for token in ("transfer", "client", "service", "request", "http", "rpc", "call")):
        return "downstream"
    return "related"


def split_camel_words(value):
    """解析或提取并返回 split_camel_words 对应的业务数据，保持现有调用约定。"""
    words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+", str(value or ""))
    return [word.lower() for word in words if word]


def infer_missing_module_names_from_text(source_text, max_modules=3):
    """查找或推断并返回 infer_missing_module_names_from_text 对应的业务数据，保持现有调用约定。"""
    text = str(source_text or "")
    names = []

    for match in re.finditer(r"https?://([^/\s:,]+)(?::\d+)?(/[^\s,\"]*)?", text, re.IGNORECASE):
        host = normalize_service_module_name(match.group(1))
        path_parts = [normalize_service_module_name(part) for part in (match.group(2) or "").split("/") if part]
        names.extend(
            candidate for candidate in [host, *(path_parts[:1])]
            if candidate and not is_infrastructure_or_bucket_name(candidate)
        )

    for match in re.finditer(r"\bcom\.zhuiyi\.([a-z0-9_]+)(?:\.([a-z0-9_]+))?", text, re.IGNORECASE):
        parts = [part for part in match.groups() if part]
        if parts:
            names.append(normalize_service_module_name("-".join(part.lower() for part in parts[:2])))

    for match in re.finditer(r"(?:离线|调用|请求|服务|模块)\s*([A-Za-z][A-Za-z0-9_-]{1,40})", text):
        names.append(match.group(1).lower())

    ignored = {
        "com", "zhuiyi", "exception", "error", "message", "http", "rpc", "desc", "code",
        "unavailable", "connection", "closed", "server", "preface", "received", "results",
        "requests", "download", "file", "please", "check", "disk", "space", "url", "facade",
    }
    deduped = []
    for name in names:
        normalized = normalize_service_module_name(name)
        if not normalized or normalized in ignored or normalized in deduped:
            continue
        deduped.append(normalized)
        if len(deduped) >= max_modules:
            break
    return deduped


def discover_related_modules_from_text(source_text, available_modules, primary_module_id=None, max_modules=8):
    """查找或推断并返回 discover_related_modules_from_text 对应的业务数据，保持现有调用约定。"""
    primary_id = str(primary_module_id or "")
    normalized_text = normalize_module_name(source_text)
    found = []
    for module in available_modules or []:
        module_id = str(module.get("module_id") or module.get("moduleId") or "")
        module_name = module.get("module_name") or module.get("moduleName") or module.get("name")
        normalized_name = normalize_module_name(module_name)
        if not module_name or not normalized_name or module_id == primary_id:
            continue
        if not module_name_matches_text(module_name, source_text):
            continue
        found.append({
            "moduleId": module_id,
            "moduleName": str(module_name),
            "role": infer_related_module_role(source_text, module_name),
            "reason": "当前日志/图片内容命中该模块名称，已自动加入上下游版本代码判断。",
        })
        if len(found) >= max_modules:
            break
    if not found:
        for module_name in infer_missing_module_names_from_text(source_text, max_modules=max_modules):
            found.append({
                "moduleId": "",
                "moduleName": module_name,
                "role": infer_related_module_role(source_text, module_name),
                "reason": "日志命中疑似上下游模块名，但当前产品仓库列表中未找到该模块仓库，请补充仓库权限或模块配置。",
            })
    return found


def build_chain_issue_context_block(lines, index, before=4, after=8):
    """构建并返回 build_chain_issue_context_block 对应的业务数据，保持现有调用约定。"""
    start = max(0, index - before)
    end = min(len(lines), index + after + 1)
    next_event_re = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}.*\b(ERROR|Exception|Error|panic:|WARN)\b", re.IGNORECASE)
    for cursor in range(index + 1, end):
        if next_event_re.search(lines[cursor]):
            end = cursor
            break
    while end < len(lines) and re.search(r"^\s+(at\s+[\w.$]+\(|Caused by:|\.\.\.\s+\d+\s+more)", lines[end]):
        end += 1
    return "\n".join(lines[start:end])


def split_chain_issue_candidates(source_text, available_modules=None, primary_module_id=None, max_candidates=50):
    """解析或提取并返回 split_chain_issue_candidates 对应的业务数据，保持现有调用约定。"""
    candidates = []
    seen = set()
    lines = [line.strip() for line in str(source_text or "").splitlines() if line.strip()]
    for line_index, line in enumerate(lines):
        if not re.search(r"\b(ERROR|Exception|Error|panic:|WARN)\b|异常|失败|错误|超时", line, re.IGNORECASE):
            continue
        issue_text = build_chain_issue_context_block(lines, line_index)
        decision = assess_chain_relevance(issue_text)
        if not decision.get("requiresRelatedEvidence"):
            continue
        related_modules = discover_related_modules_from_text(
            issue_text,
            available_modules or [],
            primary_module_id=primary_module_id,
        )
        normalized_line = normalize_module_name(line)
        related_modules.sort(
            key=lambda item: 0
            if normalize_module_name(item.get("moduleName")) in normalized_line
            else 1
        )
        module_key = ",".join(sorted(item.get("moduleName") or "" for item in related_modules))
        summary = summarize_issue_context(issue_text)
        key = (normalize_module_name(module_key), normalize_module_name(summary[:160]))
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "index": len(candidates),
            "issueSummary": summary,
            "issueContext": issue_text,
            "matchedSignals": decision.get("matchedSignals", []),
            "relatedModules": related_modules,
            "reason": decision.get("reason"),
        })
        if len(candidates) >= max_candidates:
            break
    return candidates


def build_analysis_repositories(data, task_id=None):
    """构建并返回 build_analysis_repositories 对应的业务数据，保持现有调用约定。"""
    repositories = [{
        "role": "primary",
        "moduleId": str(data.get("moduleId") or ""),
        "moduleName": data.get("moduleName") or data.get("primaryModuleName") or "primary",
        "branchAddress": data.get("branchAddress"),
        "tagVersion": data.get("tagVersion"),
        "common": bool(data.get("common")),
        "workspaceId": str(task_id or "shared"),
    }]

    for index, module in enumerate(data.get("relatedModules") or [], start=1):
        is_common = bool(module.get("common"))
        if not module.get("branchAddress") or (not module.get("tagVersion") and not is_common):
            continue
        module_id = str(module.get("moduleId") or module.get("module_id") or index)
        repositories.append({
            "role": module.get("role") or "related",
            "moduleId": module_id,
            "moduleName": module.get("moduleName") or module.get("module_name") or f"related-{index}",
            "branchAddress": module.get("branchAddress"),
            "tagVersion": module.get("tagVersion") or "",
            "common": is_common,
            "metadataOnly": is_common and not module.get("tagVersion"),
            "workspaceId": str(task_id or "shared"),
        })
    return repositories


def clone_analysis_repositories(repositories, clone_func, max_workers=4):
    """处理 clone_analysis_repositories 对应的业务步骤，并向调用方返回所需结果。"""
    cloned_repositories = []
    if not repositories:
        return cloned_repositories

    flask_app = None
    try:
        from flask import has_app_context
        if has_app_context():
            flask_app = app._get_current_object()
    except Exception:
        flask_app = None

    def run_clone(repository):
        """执行 run_clone 对应的业务数据，保持现有调用约定。"""
        if flask_app is not None:
            with flask_app.app_context():
                return clone_func(
                    repository.get("branchAddress"),
                    repository.get("tagVersion"),
                    workspace_id=repository.get("workspaceId"),
                )
        return clone_func(
            repository.get("branchAddress"),
            repository.get("tagVersion"),
            workspace_id=repository.get("workspaceId"),
        )

    clone_targets = [repository for repository in repositories if not repository.get("metadataOnly")]
    if not clone_targets:
        return cloned_repositories
    worker_count = max(1, min(int(max_workers or 1), len(clone_targets)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(run_clone, repository): repository
            for repository in clone_targets
        }
        for future in as_completed(future_map):
            repository = future_map[future]
            try:
                repo_path = future.result()
            except Exception:
                logging.exception(
                    "并发拉取代码失败：module=%s, repo=%s, version=%s",
                    repository.get("moduleName"),
                    repository.get("branchAddress"),
                    repository.get("tagVersion"),
                )
                if repository.get("role") == "primary":
                    raise
                continue
            if not repo_path:
                continue
            cloned = dict(repository)
            cloned["repo_path"] = repo_path
            cloned_repositories.append(cloned)
    return cloned_repositories


def get_available_modules_for_product(product_id):
    """读取并返回 get_available_modules_for_product 对应的业务数据，保持现有调用约定。"""
    if not product_id:
        return []
    try:
        from app.modules.models.model import Module
        from app.relasionship.models.model import ProductModule
    except ImportError:
        logging.warning("\u6a21\u5757\u6a21\u578b\u52a0\u8f7d\u5931\u8d25\uff0c\u65e0\u6cd5\u8bc6\u522b\u4e0a\u4e0b\u6e38\u6a21\u5757")
        return []

    product_modules = ProductModule.query.filter_by(product_id=product_id).all()
    modules = []
    for relation in product_modules:
        module = Module.query.get(relation.module_id)
        if module:
            modules.append({"module_id": module.id, "module_name": module.name})
    return modules


def _normalize_signal(value):
    """规范化并返回 _normalize_signal 对应的业务数据，保持现有调用约定。"""
    return re.sub(r"[^a-z0-9_./-]+", "", str(value or "").lower())


def build_related_code_signals(source_text, matched_signals=None):
    """构建并返回 build_related_code_signals 对应的业务数据，保持现有调用约定。"""
    signals = []

    url_signals = []
    for url in re.findall(r"https?://[^\s,\"']+", str(source_text or ""), flags=re.IGNORECASE):
        path = re.sub(r"^https?://[^/]+", "", url, flags=re.IGNORECASE).split("?", 1)[0]
        segments = [segment for segment in path.split("/") if segment]
        for signal in [
            segments[-1] if segments else "",
            "/".join(segments[-2:]) if len(segments) >= 2 else "",
            "/".join(segments[-3:]) if len(segments) >= 3 else "",
        ]:
            normalized = _normalize_signal(signal)
            if len(normalized) >= 5 and normalized not in url_signals:
                url_signals.append(normalized)

    for signal in url_signals + list(matched_signals or []) + RELATED_CODE_SIGNAL_PATTERNS:
        normalized = _normalize_signal(signal)
        if len(normalized) < 3:
            continue
        if normalized not in signals:
            signals.append(normalized)

    for token in re.findall(r"[A-Za-z][A-Za-z0-9_.:/-]{3,80}", str(source_text or "")):
        normalized = _normalize_signal(token)
        if (
            len(normalized) >= 5
            and any(marker in normalized for marker in ("api", "http", "rpc", "callback", "service", "client"))
            and normalized not in signals
        ):
            signals.append(normalized)
        if len(signals) >= 40:
            break
    return signals[:40]


def find_related_code_signal_hits(repo_path, signals, max_hits=8):
    """查找或推断并返回 find_related_code_signal_hits 对应的业务数据，保持现有调用约定。"""
    normalized_signals = [signal for signal in signals or [] if signal]
    hits = []
    if not normalized_signals:
        return hits

    for file_path in iter_source_files(repo_path):
        lines = read_text_lines(file_path)
        for line_index, line in enumerate(lines, start=1):
            lowered_line = line.lower()
            matched = [signal for signal in normalized_signals if signal in _normalize_signal(lowered_line)]
            if not matched:
                continue
            hits.append({
                "file": file_path,
                "line": line_index,
                "signal": matched[0],
                "content": line.strip()[:300],
            })
            if len(hits) >= max_hits:
                return hits
    return hits


def find_related_repository_code_usages(repo_path, log_content, components, max_matches=8):
    """查找或推断并返回 find_related_repository_code_usages 对应的业务数据，保持现有调用约定。"""
    component_snippets = find_component_code_usages(repo_path, components, max_matches=max_matches)
    remaining = max(0, max_matches - len(component_snippets))
    if not remaining:
        return component_snippets

    signals = build_related_code_signals(log_content)
    hits = find_related_code_signal_hits(repo_path, signals, max_hits=remaining)
    signal_errors = [
        {
            "file": hit.get("file"),
            "line": hit.get("line"),
            "error": f"上下游链路信号 {hit.get('signal')}",
            "component": hit.get("signal"),
            "reason": "日志中的接口、回调或服务链路信号在该上下游仓库中命中。",
        }
        for hit in hits
    ]
    signal_snippets = extract_code_snippets(signal_errors)
    merged = []
    seen = set()
    for snippet in component_snippets + signal_snippets:
        key = (snippet.get("file"), snippet.get("line"), snippet.get("component"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(snippet)
    return merged[:max_matches]


def assess_related_code_ownership(source_text, chain_decision, cloned_repositories):
    """执行故障分析并返回 assess_related_code_ownership 对应的业务数据，保持现有调用约定。"""
    signals = build_related_code_signals(source_text, chain_decision.get("matchedSignals"))
    related_hits = []
    primary_hits = []
    for repository in cloned_repositories or []:
        hits = find_related_code_signal_hits(repository.get("repo_path"), signals)
        if not hits:
            continue
        item = {
            "moduleId": repository.get("moduleId"),
            "moduleName": repository.get("moduleName"),
            "role": repository.get("role"),
            "hits": hits,
        }
        if repository.get("role") == "primary":
            primary_hits.append(item)
        else:
            related_hits.append(item)

    if not related_hits:
        return {
            "status": "no_related_code_signal",
            "requiresRelatedEvidence": False,
            "chainOwner": "primary",
            "message": "版本代码中未发现明确的上下游接口/回调命中，先使用当前日志/图片分析故障原因。",
            "reason": "已拉取用户选择的版本代码，但相关模块中没有命中当前报错的链路信号。",
            "codeEvidence": primary_hits,
        }

    roles = {item.get("role") for item in related_hits}
    if "upstream" in roles and "downstream" in roles:
        owner = "upstream_downstream"
        owner_label = "上游或下游"
    elif "upstream" in roles:
        owner = "upstream"
        owner_label = "上游"
    elif "downstream" in roles:
        owner = "downstream"
        owner_label = "下游"
    else:
        owner = "related"
        owner_label = "上下游关联模块"

    return {
        "status": "need_related_evidence",
        "requiresRelatedEvidence": True,
        "chainOwner": owner,
        "message": f"版本代码判断此问题可能由{owner_label}出现，请继续补充对应日志或图片。",
        "reason": "用户选择的上下游版本代码中命中了当前报错的接口调用、回调、响应或超时信号。",
        "codeEvidence": related_hits,
    }


def read_analysis_input_text(data):
    """读取并返回 read_analysis_input_text 对应的业务数据，保持现有调用约定。"""
    source_type = str(data.get("source_type") or "file").lower()
    if source_type == "image":
        return "\n".join(
            str(value or "")
            for value in [data.get("image_tag"), data.get("image_description")]
            if value
        )
    input_path = data.get("file_path")
    if not input_path or not os.path.exists(input_path):
        return ""
    try:
        with open(input_path, "r", encoding="utf-8") as file_obj:
            return file_obj.read()
    except UnicodeDecodeError:
        with open(input_path, "r", encoding="gb18030", errors="ignore") as file_obj:
            return file_obj.read()
    except OSError:
        return ""


def summarize_issue_text(text):
    """格式化或整理并返回 summarize_issue_text 对应的业务数据，保持现有调用约定。"""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    for line in lines:
        if re.search(r"\b(ERROR|Exception|Error|panic:|WARN)\b|异常|失败|错误|超时", line, re.IGNORECASE):
            return line[:500]
    return (lines[0] if lines else "")[:500]


def summarize_issue_context(text):
    """格式化或整理并返回 summarize_issue_context 对应的业务数据，保持现有调用约定。"""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return "\n".join(lines)[:500]


def assess_chain_relevance(text):
    """执行故障分析并返回 assess_chain_relevance 对应的业务数据，保持现有调用约定。"""
    source_text = str(text or "")
    lowered_text = source_text.lower()
    matched_chain = []
    for pattern in CHAIN_RELEVANCE_PATTERNS:
        match = re.search(pattern, source_text, re.IGNORECASE)
        if match:
            matched_chain.append(match.group(0))

    matched_local = []
    for pattern in LOCAL_ONLY_PATTERNS:
        match = re.search(pattern, source_text, re.IGNORECASE)
        if match:
            matched_local.append(match.group(0))

    requires_related = bool(matched_chain)
    if requires_related:
        reason = "当前日志/图片中出现接口调用、远程服务、回调、超时或上下游返回异常信号，需要补充上下游日志或截图继续判断。"
        message = "识别到此问题可能涉及上下游链路，请补充上游/下游日志或图片。"
    else:
        reason = "当前日志/图片未发现明确的接口调用、远程服务返回、回调或上下游超时信号。"
        if matched_local:
            reason += " 已发现更偏本模块内部异常的信号：" + "、".join(matched_local[:3])
        message = "此问题不涉及上下游链路判断，开始分析故障原因。"

    return {
        "status": "need_related_evidence" if requires_related else "no_related_evidence",
        "requiresRelatedEvidence": requires_related,
        "message": message,
        "reason": reason,
        "issueSummary": summarize_issue_text(source_text),
        "matchedSignals": matched_chain[:8],
        "evidenceTypes": ["log", "image"] if requires_related else [],
    }


def build_chain_relevance_input(data):
    """构建并返回 build_chain_relevance_input 对应的业务数据，保持现有调用约定。"""
    source_type = str(data.get("source_type") or "file").lower()
    if source_type == "image":
        input_paths = [path for path in (data.get("file_paths") or []) if path]
        if data.get("file_path") and data.get("file_path") not in input_paths:
            input_paths.insert(0, data.get("file_path"))
        metadata = [data.get("image_tag"), data.get("image_description")]
        metadata.extend(os.path.basename(str(path)) for path in input_paths)
        return "\n".join(str(value) for value in metadata if value)
    return read_analysis_input_text(data)


def _local_ocr_enabled():
    """判断是否启用本地 OCR；默认停用，图片识别统一交给多模态模型。"""
    configured = app.config.get("LOCAL_OCR_ENABLED")
    if configured is None:
        configured = os.getenv("LOCAL_OCR_ENABLED", "false")
    return str(configured).strip().lower() in {"1", "true", "yes", "on"}


def _gpt_vision_fallback_confidence_threshold():
    """处理 _gpt_vision_fallback_confidence_threshold 对应的业务步骤，并向调用方返回所需结果。"""
    configured = app.config.get(
        "OCR_GPT_FALLBACK_CONFIDENCE",
        os.getenv("OCR_GPT_FALLBACK_CONFIDENCE", "0.6"),
    )
    try:
        return float(configured)
    except (TypeError, ValueError):
        return 0.6


def should_use_gpt_vision_fallback(image_ocr):
    """判断 should_use_gpt_vision_fallback 对应的业务数据，保持现有调用约定。"""
    data = image_ocr if isinstance(image_ocr, dict) else {}
    if str(data.get("engine") or "").strip().lower() == "gpt-vision":
        return False
    if not _local_ocr_enabled():
        return True
    if not data.get("available"):
        return True
    if not str(data.get("extracted_text") or "").strip():
        return True
    try:
        confidence = float(data.get("average_confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return confidence < _gpt_vision_fallback_confidence_threshold()


def _empty_image_ocr_result(warning=""):
    """处理 _empty_image_ocr_result 对应的业务步骤，并向调用方返回所需结果。"""
    return {
        "available": False,
        "engine": "paddleocr",
        "extracted_text": "",
        "lines": [],
        "average_confidence": 0.0,
        "warnings": [warning] if warning else [],
    }


def build_gpt_image_ocr_result(image_paths, image_tag, image_description="", image_fallback_extractor=None):
    """构建并返回 build_gpt_image_ocr_result 对应的业务数据，保持现有调用约定。"""
    image_paths = [path for path in (image_paths or []) if path]
    if not image_paths:
        return {
            "available": False,
            "engine": "gpt-vision",
            "extracted_text": "",
            "lines": [],
            "average_confidence": 0.0,
            "warnings": ["no image files available for GPT vision fallback"],
        }
    fallback_extractor = image_fallback_extractor or analyze_uploaded_image
    try:
        image_analysis = fallback_extractor(image_paths, image_tag, image_description)
        normalized = normalize_image_analysis(image_tag, image_analysis, image_description=image_description)
        extracted_text = normalized.get("extracted_text") or normalized.get("summary") or ""
        result = {
            "available": bool(extracted_text),
            "engine": "gpt-vision",
            "extracted_text": extracted_text,
            "lines": [],
            "average_confidence": 0.0,
            "warnings": [],
            "summary": normalized.get("summary"),
            "keywords": normalized.get("keywords"),
            "components": normalized.get("components"),
            "scene_summary": normalized.get("scene_summary"),
            "business_domain": normalized.get("business_domain"),
            "page_name": normalized.get("page_name"),
            "user_action": normalized.get("user_action"),
            "error_message": normalized.get("error_message"),
            "visible_fields": normalized.get("visible_fields"),
            "candidate_apis": normalized.get("candidate_apis"),
            "candidate_code_keywords": normalized.get("candidate_code_keywords"),
            "missing_context": normalized.get("missing_context"),
            "analysis_text": normalized.get("analysis_text"),
        }
        if not result["available"]:
            result["warnings"].append("GPT vision fallback returned no usable text")
        return result
    except Exception as exc:
        logging.exception("GPT vision fallback failed")
        return {
            "available": False,
            "engine": "gpt-vision",
            "extracted_text": "",
            "lines": [],
            "average_confidence": 0.0,
            "warnings": [str(exc)],
        }


def normalize_image_ocr_payload(payload):
    """规范化并返回 normalize_image_ocr_payload 对应的业务数据，保持现有调用约定。"""
    data = payload if isinstance(payload, dict) else {}
    lines = []
    for item in data.get("lines") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        try:
            confidence = float(item.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        lines.append({
            "text": text,
            "confidence": confidence,
            "image_index": int(item.get("image_index") or 0),
            "line_index": int(item.get("line_index") or 0),
        })
    try:
        average_confidence = float(data.get("average_confidence") or 0.0)
    except (TypeError, ValueError):
        average_confidence = 0.0
    return {
        "available": bool(data.get("available")),
        "engine": str(data.get("engine") or "paddleocr"),
        "extracted_text": str(data.get("extracted_text") or "").strip(),
        "lines": lines,
        "average_confidence": average_confidence,
        "warnings": [str(item) for item in (data.get("warnings") or []) if str(item or "").strip()],
    }


def build_chain_relevance_context(data, ocr_extractor=None, image_fallback_extractor=None):
    """构建并返回 build_chain_relevance_context 对应的业务数据，保持现有调用约定。"""
    metadata_text = build_chain_relevance_input(data)
    if str(data.get("source_type") or "file").lower() != "image":
        return metadata_text, None

    supplied_ocr_source = data.get("image_ocr")
    supplied_ocr = normalize_image_ocr_payload(supplied_ocr_source)
    has_supplied_ocr = isinstance(supplied_ocr_source, dict)
    if supplied_ocr.get("engine") == "gpt-vision" and supplied_ocr.get("extracted_text"):
        return "\n".join(filter(None, [supplied_ocr["extracted_text"], metadata_text])), supplied_ocr
    if has_supplied_ocr and supplied_ocr.get("extracted_text") and not should_use_gpt_vision_fallback(supplied_ocr):
        return "\n".join(filter(None, [supplied_ocr["extracted_text"], metadata_text])), supplied_ocr

    input_paths = [path for path in (data.get("file_paths") or []) if path]
    if data.get("file_path") and data.get("file_path") not in input_paths:
        input_paths.insert(0, data.get("file_path"))

    image_tag = str(data.get("image_tag") or "").strip()
    image_description = str(data.get("image_description") or "").strip()

    if has_supplied_ocr and should_use_gpt_vision_fallback(supplied_ocr):
        ocr_result = build_gpt_image_ocr_result(
            input_paths,
            image_tag,
            image_description,
            image_fallback_extractor=image_fallback_extractor,
        )
        if ocr_result.get("available"):
            return "\n".join(filter(None, [ocr_result.get("extracted_text"), metadata_text])), ocr_result
        if supplied_ocr.get("extracted_text"):
            return "\n".join(filter(None, [supplied_ocr["extracted_text"], metadata_text])), supplied_ocr
        warnings = supplied_ocr.get("warnings") or []
        if ocr_result.get("warnings"):
            warnings = [*warnings, *ocr_result.get("warnings")]
        return metadata_text, {
            **ocr_result,
            "warnings": warnings,
        }

    if not has_supplied_ocr and not _local_ocr_enabled():
        ocr_result = build_gpt_image_ocr_result(
            input_paths,
            image_tag,
            image_description,
            image_fallback_extractor=image_fallback_extractor,
        )
        if ocr_result.get("available"):
            return "\n".join(filter(None, [ocr_result.get("extracted_text"), metadata_text])), ocr_result
        return metadata_text, ocr_result

    try:
        if ocr_extractor is None:
            from app.analysis.image_ocr import extract_text_from_images
            ocr_extractor = extract_text_from_images
        minimum_confidence = float(
            app.config.get("OCR_MIN_CONFIDENCE", os.getenv("OCR_MIN_CONFIDENCE", "0.45"))
        )
        try:
            raw_result = ocr_extractor(input_paths, min_confidence=minimum_confidence)
        except TypeError:
            raw_result = ocr_extractor(input_paths)
        ocr_result = normalize_image_ocr_payload(raw_result)
        if should_use_gpt_vision_fallback(ocr_result):
            gpt_result = build_gpt_image_ocr_result(
                input_paths,
                image_tag,
                image_description,
                image_fallback_extractor=image_fallback_extractor,
            )
            if gpt_result.get("available"):
                ocr_result = gpt_result
    except Exception as exc:
        logging.exception("图片来源文字提取失败（本地 OCR 通道，默认已停用）")
        ocr_result = _empty_image_ocr_result(str(exc))
    source_text = "\n".join(filter(None, [ocr_result.get("extracted_text"), metadata_text]))
    return source_text, ocr_result


def cleanup_discovery_workspace(repo_path, workspace_id, repo_base_dir=None):
    """清理 cleanup_discovery_workspace 对应的业务数据，保持现有调用约定。"""
    # 未显式传入时按 REPO_CACHE_DIR 配置解析，避免调用方与配置里写的目录不一致。
    repo_base_dir = repo_base_dir or _repo_cache_dir()
    if not repo_path or not workspace_id:
        return False
    real_base = os.path.realpath(repo_base_dir)
    workspace_path = os.path.realpath(os.path.join(real_base, str(workspace_id)))
    real_repo = os.path.realpath(repo_path)
    try:
        if (
            os.path.basename(workspace_path) == str(workspace_id)
            and os.path.commonpath([workspace_path, real_base]) == real_base
            and os.path.commonpath([real_repo, workspace_path]) == workspace_path
            and os.path.isdir(workspace_path)
        ):
            shutil.rmtree(workspace_path)
            return True
    except (OSError, ValueError):
        logging.exception("\u6e05\u7406\u5173\u8054\u6a21\u5757\u8bc6\u522b\u4e34\u65f6\u4ed3\u5e93\u5931\u8d25\uff1a%s", workspace_path)
    return False


@analysis_bp.route("/discover_related_modules", methods=["POST"])
def discover_related_modules_route():
    """查找或推断并返回 discover_related_modules_route 对应的业务数据，保持现有调用约定。"""
    data = request.json or {}
    source_type = str(data.get("source_type") or "file").lower()
    if source_type == "image" and data.get("file_paths") and not data.get("file_path"):
        data["file_path"] = data.get("file_paths")[0]

    required_fields = ["productId", "moduleId", "branchAddress", "tagVersion"]
    missing_fields = [field for field in required_fields if not data.get(field)]
    if missing_fields:
        return jsonify({"error": "\u7f3a\u5c11\u5fc5\u8981\u5b57\u6bb5", "missing_fields": missing_fields}), 400

    source_text, image_ocr = build_chain_relevance_context(data)
    decision = assess_chain_relevance(source_text)
    if image_ocr is not None:
        decision["imageOcr"] = image_ocr
    if source_type == "image" and image_ocr is not None and not image_ocr.get("available"):
        warnings = image_ocr.get("warnings") or []
        decision.update({
            "status": "image_ocr_unavailable",
            "requiresRelatedEvidence": False,
            "chainAssessmentComplete": False,
            "message": "图片识别模型未返回可用结果，无法在分析前判断上下游链路；将保留原图进入综合分析。",
            "reason": "；".join(str(item) for item in warnings if str(item).strip())
            or "图片识别模型未返回可用结果。",
        })
        return jsonify(decision)
    if decision.get("requiresRelatedEvidence"):
        available_modules = get_available_modules_for_product(data.get("productId"))
        issue_candidates = split_chain_issue_candidates(
            source_text,
            available_modules,
            primary_module_id=data.get("moduleId"),
        )
        if issue_candidates:
            first_candidate = issue_candidates[0]
            decision["issueSummary"] = first_candidate.get("issueSummary") or decision.get("issueSummary")
            decision["matchedSignals"] = first_candidate.get("matchedSignals") or decision.get("matchedSignals", [])
            decision["relatedModules"] = first_candidate.get("relatedModules", [])
            decision["issueCandidates"] = issue_candidates
        else:
            decision["relatedModules"] = discover_related_modules_from_text(
                source_text,
                available_modules,
                primary_module_id=data.get("moduleId"),
            )
            decision["issueCandidates"] = [{
                "index": 0,
                "issueSummary": decision.get("issueSummary"),
                "matchedSignals": decision.get("matchedSignals", []),
                "relatedModules": decision.get("relatedModules", []),
                "reason": decision.get("reason"),
            }]
    return jsonify(decision)


@analysis_bp.route("/assess_related_code", methods=["POST"])
def assess_related_code_route():
    """执行故障分析并返回 assess_related_code_route 对应的业务数据，保持现有调用约定。"""
    data = request.json or {}
    source_type = str(data.get("source_type") or "file").lower()
    if source_type == "image" and data.get("file_paths") and not data.get("file_path"):
        data["file_path"] = data.get("file_paths")[0]

    required_fields = ["productId", "moduleId", "branchAddress", "tagVersion"]
    missing_fields = [field for field in required_fields if not data.get(field)]
    if missing_fields:
        return jsonify({"error": "缺少必要字段", "missing_fields": missing_fields}), 400

    source_text, image_ocr = build_chain_relevance_context(data)
    chain_decision = assess_chain_relevance(source_text)
    if image_ocr is not None:
        chain_decision["imageOcr"] = image_ocr
    if not chain_decision.get("requiresRelatedEvidence"):
        return jsonify(chain_decision)

    related_modules = data.get("relatedModules") or []
    if not related_modules:
        return jsonify({
            "status": "need_related_code",
            "requiresRelatedEvidence": False,
            "requiresRelatedCode": True,
            "message": "此问题可能涉及上下游链路，请先选择上下游模块的发布分支/Tag。",
            "reason": chain_decision.get("reason"),
            "issueSummary": chain_decision.get("issueSummary"),
            "matchedSignals": chain_decision.get("matchedSignals", []),
        })

    workspace_id = f"related-code-{hashlib.md5(json.dumps(data, sort_keys=True, default=str).encode('utf-8')).hexdigest()[:12]}"
    repositories = build_analysis_repositories(data, workspace_id)
    cloned_repositories = clone_analysis_repositories(repositories, clone_git_repo, max_workers=4)
    try:
        assessment = assess_related_code_ownership(source_text, chain_decision, cloned_repositories)
        cloned_keys = {
            (repository.get("role"), str(repository.get("moduleId")), repository.get("branchAddress"), repository.get("tagVersion"))
            for repository in cloned_repositories
        }
        repository_issues = []
        for repository in repositories:
            key = (repository.get("role"), str(repository.get("moduleId")), repository.get("branchAddress"), repository.get("tagVersion"))
            if repository.get("role") == "primary" or key in cloned_keys:
                continue
            repository_issues.append({
                "moduleId": repository.get("moduleId"),
                "moduleName": repository.get("moduleName"),
                "role": repository.get("role"),
                "branchAddress": repository.get("branchAddress"),
                "tagVersion": repository.get("tagVersion"),
                "message": "模块代码拉取失败，可能是仓库不存在、分支/Tag 不存在或当前 Git 账号无权限。",
            })
        if repository_issues:
            assessment["repositoryIssues"] = repository_issues
            if not assessment.get("requiresRelatedEvidence"):
                assessment["message"] = "部分上下游模块代码拉取失败，请补充仓库权限或跳过该模块后继续分析。"
        assessment["issueSummary"] = chain_decision.get("issueSummary")
        assessment["matchedSignals"] = chain_decision.get("matchedSignals", [])
        assessment["repositories"] = [
            {
                "moduleId": repository.get("moduleId"),
                "moduleName": repository.get("moduleName"),
                "role": repository.get("role"),
                "branchAddress": repository.get("branchAddress"),
                "tagVersion": repository.get("tagVersion"),
            }
            for repository in repositories
        ]
        return jsonify(assessment)
    finally:
        for repository in cloned_repositories:
            cleanup_discovery_workspace(repository.get("repo_path"), workspace_id)


def annotate_code_snippets(snippets, repository):
    """格式化或整理并返回 annotate_code_snippets 对应的业务数据，保持现有调用约定。"""
    annotated = []
    for snippet in snippets or []:
        if not isinstance(snippet, dict):
            continue
        item = dict(snippet)
        item["module_role"] = repository.get("role")
        item["module_id"] = repository.get("moduleId")
        item["module_name"] = repository.get("moduleName")
        annotated.append(item)
    return annotated


def merge_code_snippets(*snippet_groups):
    """合并整理并返回 merge_code_snippets 对应的业务数据，保持现有调用约定。"""
    merged = []
    seen = set()
    for snippets in snippet_groups:
        for snippet in snippets or []:
            key = (snippet.get("file"), snippet.get("line"), snippet.get("component"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(snippet)
    return merged


def format_numbered_snippet(snippet_lines, start_line, target_line):
    """格式化或整理并返回 format_numbered_snippet 对应的业务数据，保持现有调用约定。"""
    formatted_lines = []
    for offset, raw_line in enumerate(snippet_lines):
        line_number = start_line + offset
        marker = ">>" if line_number == target_line else "  "
        formatted_lines.append(f"{marker} {line_number:4d} | {raw_line.rstrip()}")
    return "\n".join(formatted_lines)


def build_code_findings(code_snippets):
    """构建并返回 build_code_findings 对应的业务数据，保持现有调用约定。"""
    findings = []
    for snippet in code_snippets or []:
        if not isinstance(snippet, dict):
            continue
        file_path = snippet.get("file") or "\u672a\u77e5\u6587\u4ef6"
        line = snippet.get("line")
        code = snippet.get("numbered_snippet") or snippet.get("snippet") or ""
        if not code:
            continue
        reason = snippet.get("reason") or f"\u65e5\u5fd7\u5806\u6808\u5b9a\u4f4d\u5230 {file_path} \u7b2c {line} \u884c\uff0c\u9700\u4f18\u5148\u68c0\u67e5\u8be5\u884c\u9644\u8fd1\u4ee3\u7801\u3002"
        findings.append({
            "file": file_path,
            "line": line,
            "reason": reason,
            "code": code,
            "component": snippet.get("component"),
            "module_role": snippet.get("module_role"),
            "module_id": snippet.get("module_id"),
            "module_name": snippet.get("module_name"),
        })
    return findings


def build_common_component_reference_context(common_repositories):
    """把已声明的公共组件整理成引用关系，避免把它们误当成需要 Tag 的可拉取仓库。"""
    components = []
    for repository in common_repositories or []:
        if not isinstance(repository, dict) or not repository.get("common"):
            continue
        name = str(repository.get("moduleName") or repository.get("moduleId") or "").strip()
        if not name:
            continue
        address = str(repository.get("branchAddress") or "").strip()
        label = f"{name}（仓库：{address}）" if address else name
        if label not in components:
            components.append(label)
    if not components:
        return ""
    joined = "、".join(components)
    return (
        "\u3010公共组件引用\u3011\n"
        f"代码分析发现可能引用公共组件：{joined}。\n"
        "公共组件不要求精准 Tag，也不按主模块或非公共关联模块处理；仅在日志或主模块代码确实"
        "存在引用时，才在结论中写明“本缺陷引用了 xxx 公共组件功能，关键代码在 <file>:<line>”。\n"
        "如果无法定位关键代码，必须明确说明无法定位，不得编造引用关系或代码位置。\n\n"
    )


def analyze_code_with_deepseek(
    log_content,
    log_analysis,
    code_snippets,
    image_analysis=None,
    image_paths=None,
    common_repositories=None,
):
    """执行故障分析并返回 analyze_code_with_deepseek 对应的业务数据，保持现有调用约定。"""
    logging.info("==> \u8fdb\u5165 analyze_code_with_deepseek")
    context_lines = []
    for snippet in code_snippets:
        module_label = ""
        if snippet.get("module_name"):
            module_role = snippet.get("module_role") or "related"
            module_label = f"[{module_role} {snippet.get('module_name')}] "
        context_lines.append(
            f"{module_label}\u6587\u4ef6\uff1a{snippet['file']} \u7b2c {snippet['line']} \u884c\u9644\u8fd1\n```\n{snippet.get('numbered_snippet') or snippet['snippet']}\n```"
        )
    context = "\n".join(context_lines)
    fallback_context = "\u672a\u5b9a\u4f4d\u5230\u76f8\u5173\u4ee3\u7801\u7247\u6bb5"
    log_context = build_compact_log_context(log_content)
    image_context = ""
    image_error_instruction = ""
    if image_analysis:
        image_context = f"\n\n\u3010\u56fe\u7247\u8bc6\u522b\u6458\u8981\u3011\n{build_image_analysis_context(image_analysis, image_analysis.get('image_description', ''))}"
    if image_paths:
        image_labels = "、".join(f"第{index}张" for index in range(1, len(image_paths) + 1))
        image_error_instruction = (
            f"当前请求包含 {len(image_paths)} 张原始图片（{image_labels}）。"
            "不能因为图片识别未提取到文字或初步日志分析为 0 个问题，就判断图片中没有错误；"
            "必须逐个识别原图中所有可见的错误、异常提示和失败状态，并在每个 issue 的 evidence 中"
            "注明来源图片序号。每张图片至少要有一个 issue 覆盖；只有明确确认是同一错误的重复截图时"
            "才允许合并，并说明合并依据。即使 image_tag 是 business_image，只要画面中存在日志、"
            "异常栈或明确报错文本，也必须按错误截图处理。\n"
        )

    prompt = (
        "\u8bf7\u57fa\u4e8e\u3010\u5b8c\u6574\u65e5\u5fd7\u4e0a\u4e0b\u6587\u3011\u3001\u3010\u65e5\u5fd7\u521d\u6b65\u5206\u6790\u3011\u548c\u3010\u76f8\u5173\u4ee3\u7801\u7247\u6bb5\u3011\u505a\u6700\u7ec8\u7ed3\u8bba\u3002\n"
        "\u91cd\u8981\u539f\u5219\uff1a\u4e0d\u8981\u53ea\u6839\u636e ERROR \u884c\u5224\u65ad\uff1b\u5fc5\u987b\u7ed3\u5408\u9519\u8bef\u524d\u540e\u7684 INFO/WARN/DEBUG\u3001\u8bf7\u6c42\u94fe\u8def\u3001\u914d\u7f6e\u52a0\u8f7d\u3001\u8fde\u63a5\u72b6\u6001\u3001\u91cd\u8bd5\u3001\u8017\u65f6\u548c\u8d44\u6e90\u53d8\u5316\u3002\n"
        "\u4ee3\u7801\u7247\u6bb5\u662f\u5173\u952e\u8bc1\u636e\uff0c\u4f46\u6ca1\u6709\u4ee3\u7801\u8bc1\u636e\u5e76\u4e0d\u4ee3\u8868\u4e00\u5b9a\u4e0d\u662f\u914d\u7f6e\u3001\u7f51\u7edc\u3001\u6570\u636e\u3001\u4f9d\u8d56\u6216\u8d44\u6e90\u95ee\u9898\u3002\n"
        "\u5982\u679c\u5224\u65ad\u4e3a\u4ee3\u7801\u95ee\u9898\uff0c\u5fc5\u987b\u6307\u51fa\u5177\u4f53\u6587\u4ef6\u3001\u884c\u53f7\u3001\u76f8\u5173\u4ee3\u7801\u5757\u548c\u4e3a\u4ec0\u4e48\u8fd9\u6bb5\u4ee3\u7801\u4f1a\u89e6\u53d1\u65e5\u5fd7\u4e2d\u7684\u73b0\u8c61\u3002\n"
        "\u5982\u679c\u4ee3\u7801\u7247\u6bb5\u6765\u81ea\u591a\u4e2a\u6a21\u5757\uff0c\u5fc5\u987b\u533a\u5206\u4e3b\u6a21\u5757\u3001\u4e0a\u4e0b\u6e38/\u5173\u8054\u6a21\u5757\u7684\u8d23\u4efb\u8fb9\u754c\uff0c\u8bf4\u660e\u662f\u53c2\u6570\u4f20\u9012\u3001\u53d1\u5e03/\u8c03\u5ea6\u3001\u56de\u8c03\u5904\u7406\u8fd8\u662f\u4e3b\u6a21\u5757\u81ea\u8eab\u903b\u8f91\u89e6\u53d1\u3002\n"
        "\u5982\u679c\u5224\u65ad\u53ef\u80fd\u662f\u914d\u7f6e/\u7f51\u7edc/\u6570\u636e/\u4f9d\u8d56/\u8d44\u6e90\u95ee\u9898\uff0c\u4e5f\u8981\u8bf4\u660e\u65e5\u5fd7\u4f9d\u636e\u4ee5\u53ca\u9700\u8981\u8865\u5145\u68c0\u67e5\u7684\u914d\u7f6e\u9879\u3001\u7f51\u7edc\u94fe\u8def\u3001\u6570\u636e\u6837\u672c\u6216\u5916\u90e8\u670d\u52a1\u3002\n"
        f"{image_error_instruction}"
        "必须把【全量错误清单】里的错误按语义分组为 issues 数组。每个独立问题输出为一个对象：问题1、问题2、问题3...；不要只给一个总括结论。\n"
        "每个 issue 必须包含 title、summary、root_cause、solution、evidence、query_commands、fix_commands、code_locations。code_locations 里写明 file、line、reason；如果没有代码证据也必须说明 reason。\n"
        "\u8bf7\u53ea\u8fd4\u56de JSON\uff0c\u4e0d\u8981\u8fd4\u56de Markdown\u3002JSON \u5b57\u6bb5\u5fc5\u987b\u5305\u542b\uff1a\n"
        "- issue_category: code_issue/data_issue/config_issue/network_issue/dependency_issue/resource_issue/unknown \u4e4b\u4e00\n"
        "- conclusion_summary: \u4e00\u53e5\u8bdd\u7ed3\u8bba\n"
        "- root_cause: \u6839\u56e0\u8bf4\u660e\uff0c\u5fc5\u987b\u8bf4\u660e\u7ed3\u8bba\u662f\u5426\u7531\u4ee3\u7801\u7247\u6bb5\u652f\u6491\n"
        "- solution: \u5904\u7406\u5efa\u8bae\n"
        "- confidence: 0 \u5230 1 \u7684\u6570\u5b57\n"
        "- possible_causes: \u5bf9\u8c61\uff0c\u5fc5\u987b\u5305\u542b code_issue/config_issue/network_issue/data_issue/dependency_issue/resource_issue \u516d\u4e2a\u952e\uff1b\u6bcf\u4e2a\u952e\u7684\u503c\u90fd\u662f {possible: boolean, confidence: 0\u52301, reason: string}\n"
        "- query_commands: \u5b57\u7b26\u4e32\u6570\u7ec4\uff0c\u7ed9\u51fa\u53ef\u76f4\u63a5\u6267\u884c\u7684\u6392\u67e5/\u67e5\u8be2\u547d\u4ee4\uff0c\u6bd4\u5982 grep\u3001redis-cli\u3001mysql\u3001curl\u3001kubectl \u7b49\n"
        "- fix_commands: \u5b57\u7b26\u4e32\u6570\u7ec4\uff0c\u7ed9\u51fa\u53ef\u76f4\u63a5\u6267\u884c\u7684\u4fee\u590d/\u7f13\u89e3\u547d\u4ee4\uff0c\u5982\u679c\u4e0d\u5e94\u76f4\u63a5\u6267\u884c\u5219\u7ed9\u51fa\u5b89\u5168\u66ff\u4ee3\u65b9\u6848\n"
        "- evidence: \u5b57\u7b26\u4e32\u6570\u7ec4\uff0c\u5217\u51fa\u5224\u65ad\u4f9d\u636e\uff0c\u4f18\u5148\u5305\u542b\u65e5\u5fd7\u4e0a\u4e0b\u6587\u3001\u4ee3\u7801\u6587\u4ef6\u3001\u884c\u53f7\u3001\u4ee3\u7801\u7247\u6bb5\u8bf4\u660e\n\n"
        "- issues: 数组，每个元素表示一个独立问题，字段包含 title、issue_category、summary、root_cause、solution、confidence、evidence、query_commands、fix_commands、code_locations\n\n"
        f"{log_context}\n\n"
        f"\u3010\u65e5\u5fd7\u521d\u6b65\u5206\u6790\u3011\n{log_analysis}"
        f"{image_context}\n\n"
        f"{build_common_component_reference_context(common_repositories)}"
        f"\u3010\u76f8\u5173\u4ee3\u7801\u7247\u6bb5\u3011\n{context or fallback_context}"
    )
    logging.info(
        "\u7efc\u5408\u5206\u6790\u63d0\u793a\u8bcd\u5df2\u751f\u6210\uff1acode_snippet_count=%s, has_image_context=%s, log_context_chars=%s",
        len(code_snippets),
        bool(image_analysis),
        len(log_context),
    )

    try:
        logging.info("\u8c03\u7528 AI \u6267\u884c\u4ee3\u7801\u4e0e\u65e5\u5fd7\u7684\u7efc\u5408\u5206\u6790")
        system_prompt = "\u4f60\u662f\u4e00\u4e2a\u7ecf\u9a8c\u4e30\u5bcc\u7684\u540e\u7aef\u6545\u969c\u5b9a\u4f4d\u4e13\u5bb6\u3002"
        if image_paths:
            image_data_urls = [build_image_data_url(path) for path in image_paths if path]
            result = call_ai_multimodal_model(system_prompt, prompt, image_data_urls)
        else:
            result = call_ai_model(system_prompt, prompt)
        logging.info("AI \u7efc\u5408\u5206\u6790\u6210\u529f")
        return result
    except AiServiceError:
        raise
    except Exception as exc:
        logging.exception("\u8c03\u7528 AI \u8fdb\u884c\u4ee3\u7801\u5206\u6790\u5931\u8d25\uff1a%s", exc)
        return "\u4ee3\u7801\u5206\u6790\u5931\u8d25\uff0c\u65e0\u6cd5\u5b9a\u4f4d\u5177\u4f53\u9519\u8bef"


def insert_query_record(data, answer_status, **extra):
    """保存 insert_query_record 对应的业务数据，保持现有调用约定。"""
    try:
        query_record = QueryRecord(
            product_id=data.get("productId"),
            module_id=data.get("moduleId"),
            log_id=data.get("log_id") or data.get("logId"),
            log_file_path=data.get("file_path"),
            branch_url=data.get("branchAddress"),
            branch_version=data.get("tagVersion"),
            created_at=datetime.utcnow(),
            answer=answer_status,
            status=extra.get("status") or ("success" if answer_status == 0 else "failed"),
            log_hash=extra.get("log_hash"),
            error_fingerprint=extra.get("error_fingerprint"),
            duration_ms=extra.get("duration_ms"),
            model_name=app.config.get("OPENAI_MODEL"),
            hit_cache=bool(extra.get("hit_cache", False)),
            knowledge_case_id=extra.get("knowledge_case_id"),
        )
        db.session.add(query_record)
        db.session.commit()
        logging.info("\u67e5\u8be2\u8bb0\u5f55\u5199\u5165\u6210\u529f\uff1a%s", query_record)
    except Exception as exc:
        logging.exception("\u63d2\u5165\u67e5\u8be2\u8bb0\u5f55\u5931\u8d25\uff1a%s", exc)
        db.session.rollback()
