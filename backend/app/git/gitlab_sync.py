"""gitlab sync 模块负责本文件相关的业务流程、数据转换与依赖协作。"""
import base64
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.client import RemoteDisconnected
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.parse import urljoin

from sqlalchemy.exc import SQLAlchemyError

from app.branches.models.model import Branch
from app.modules.models import Module
from app.product.models import Product
from app.relasionship.models.model import ModuleBranch, ProductModule
from extensions import db


class GitLabSyncError(RuntimeError):
    """GitLabSyncError 类封装该领域对象的状态、依赖与相关行为。"""
    def __init__(self, message, status_code=None):
        """初始化当前对象的依赖、界面状态或运行参数。"""
        super().__init__(message)
        self.status_code = status_code


def normalize_gitlab_project(project):
    """规范化并返回 normalize_gitlab_project 对应的业务数据，保持现有调用约定。"""
    path_with_namespace = project.get("path_with_namespace") or project.get("path") or project.get("name")
    parts = [part for part in path_with_namespace.split("/") if part]
    product_name = parts[0] if parts else project.get("namespace", {}).get("name") or "default"

    repo_url = project.get("http_url_to_repo") or project.get("ssh_url_to_repo")
    if not repo_url and project.get("web_url"):
        repo_url = f"{project['web_url'].rstrip('/')}.git"

    return {
        "product_name": product_name,
        "module_name": project.get("name") or parts[-1],
        "repo_url": repo_url,
        "default_branch": project.get("default_branch"),
        "project_id": project.get("id"),
        "path_with_namespace": path_with_namespace,
    }


def build_gitlab_project_query(page, membership_only=False):
    """构建并返回 build_gitlab_project_query 对应的业务数据，保持现有调用约定。"""
    params = {
        "simple": "true",
        "per_page": 100,
        "page": page,
        "order_by": "path",
        "sort": "asc",
    }
    if membership_only:
        params["membership"] = "true"
    return params


def fetch_gitlab_projects(
    base_url,
    username=None,
    password=None,
    private_token=None,
    timeout=30,
    membership_only=False,
    max_retries=3,
    retry_delay=1,
    page_concurrency=1,
):
    """并发拉取 GitLab 分页数据，写库阶段仍由调用方串行执行。"""
    api_url = urljoin(base_url.rstrip("/") + "/", "api/v4/projects")
    auth_headers = _build_gitlab_auth_headers(username, password, private_token)
    concurrency = max(1, int(page_concurrency or 1))
    started_at = time.perf_counter()
    logging.info(
        "GitLab 项目拉取开始：host=%s membership_only=%s page_concurrency=%s",
        urlparse(base_url).netloc or base_url,
        membership_only,
        concurrency,
    )

    first_batch, first_headers = _fetch_gitlab_page(
        api_url,
        1,
        membership_only,
        auth_headers,
        timeout,
        max_retries,
        retry_delay,
    )
    pages = {1: first_batch}
    total_pages = _parse_page_header(first_headers.get("X-Total-Pages"))
    next_page = _parse_page_header(first_headers.get("X-Next-Page"))

    if total_pages is None:
        while next_page and next_page not in pages:
            batch, response_headers = _fetch_gitlab_page(
                api_url,
                next_page,
                membership_only,
                auth_headers,
                timeout,
                max_retries,
                retry_delay,
            )
            pages[next_page] = batch
            next_page = _parse_page_header(response_headers.get("X-Next-Page"))
    elif total_pages > 1:
        remaining_pages = list(range(2, total_pages + 1))
        if concurrency > 1:
            workers = min(concurrency, len(remaining_pages))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gitlab-sync-page") as executor:
                futures = {
                    executor.submit(
                        _fetch_gitlab_page,
                        api_url,
                        page,
                        membership_only,
                        auth_headers,
                        timeout,
                        max_retries,
                        retry_delay,
                    ): page
                    for page in remaining_pages
                }
                for future in as_completed(futures):
                    page = futures[future]
                    pages[page], _ = future.result()
                    logging.info("GitLab 项目分页拉取完成：page=%s projects=%s", page, len(pages[page]))
        else:
            for page in remaining_pages:
                pages[page], _ = _fetch_gitlab_page(
                    api_url,
                    page,
                    membership_only,
                    auth_headers,
                    timeout,
                    max_retries,
                    retry_delay,
                )
                logging.info("GitLab 项目分页拉取完成：page=%s projects=%s", page, len(pages[page]))

    projects = []
    for page in sorted(pages):
        projects.extend(pages[page])

    logging.info(
        "GitLab 项目拉取完成：projects=%s pages=%s duration_ms=%s",
        len(projects),
        len(pages),
        int((time.perf_counter() - started_at) * 1000),
    )
    return projects


def _build_gitlab_auth_headers(username, password, private_token):
    """构建 GitLab 请求头，不在日志中输出任何凭据。"""
    headers = {}
    if private_token:
        headers["PRIVATE-TOKEN"] = private_token
    elif username or password:
        raw_token = f"{username or ''}:{password or ''}".encode("utf-8")
        headers["Authorization"] = f"Basic {base64.b64encode(raw_token).decode('ascii')}"
    return headers


def _fetch_gitlab_page(
    api_url,
    page,
    membership_only,
    headers,
    timeout,
    max_retries,
    retry_delay,
):
    params = build_gitlab_project_query(page, membership_only=membership_only)
    batch, response_headers = _get_json_with_retry(
        api_url,
        params,
        headers,
        timeout,
        max_retries=max_retries,
        retry_delay=retry_delay,
    )
    if not isinstance(batch, list):
        raise GitLabSyncError("GitLab API 返回的项目数据格式不正确")
    return batch, response_headers


def _parse_page_header(value):
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _get_json_with_retry(api_url, params, headers, timeout, max_retries, retry_delay):
    """读取并返回 _get_json_with_retry 对应的业务数据，保持现有调用约定。"""
    attempt = 0
    while True:
        attempt += 1
        try:
            return _get_json(api_url, params, headers, timeout)
        except (TimeoutError, RemoteDisconnected, GitLabSyncError) as exc:
            if isinstance(exc, GitLabSyncError) and exc.status_code:
                raise
            if attempt > max_retries:
                raise GitLabSyncError("GitLab API 请求超时或连接失败，请稍后重试") from exc
            logging.warning(
                "GitLab API 请求失败，准备重试：page=%s attempt=%s max_retries=%s error=%s",
                params.get("page"),
                attempt,
                max_retries,
                type(exc).__name__,
            )
            if retry_delay:
                time.sleep(retry_delay)


def _get_json(api_url, params, headers, timeout):
    """读取并返回 _get_json 对应的业务数据，保持现有调用约定。"""
    url = f"{api_url}?{urlencode(params)}"
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload), response.headers
    except HTTPError as exc:
        raise GitLabSyncError("GitLab API 返回错误", status_code=exc.code) from exc
    except URLError as exc:
        raise GitLabSyncError("无法连接 GitLab API") from exc


def sync_gitlab_projects(projects, db_max_retries=2):
    """更新 sync_gitlab_projects 对应的业务数据，保持现有调用约定。"""
    stats = {
        "scanned_projects": 0,
        "synced_projects": 0,
        "skipped_projects": 0,
        "created_products": 0,
        "created_modules": 0,
        "created_branches": 0,
        "created_product_modules": 0,
        "created_module_branches": 0,
    }

    logging.info("GitLab 项目写库开始：projects=%s db_max_retries=%s", len(projects), db_max_retries)
    for project in projects:
        stats["scanned_projects"] += 1
        item = normalize_gitlab_project(project)
        if not item["product_name"] or not item["module_name"] or not item["repo_url"]:
            stats["skipped_projects"] += 1
            logging.warning("GitLab 项目字段不完整，已跳过：path=%s", item["path_with_namespace"])
            continue

        project_stats = _sync_project_with_retry(item, db_max_retries)
        for key, value in project_stats.items():
            stats[key] += value
        if stats["scanned_projects"] % 50 == 0:
            logging.info("GitLab 项目写库进度：scanned=%s total=%s", stats["scanned_projects"], len(projects))

    logging.info("GitLab 项目写库完成：%s", stats)
    return stats


def _sync_project_with_retry(item, db_max_retries):
    """更新 _sync_project_with_retry 对应的业务数据，保持现有调用约定。"""
    attempt = 0
    while True:
        attempt += 1
        try:
            project_stats = _sync_normalized_project(item)
            db.session.commit()
            return project_stats
        except SQLAlchemyError:
            db.session.rollback()
            _dispose_current_engine()
            if attempt > db_max_retries:
                raise
            logging.warning(
                "GitLab 项目写库失败，准备重试：project=%s attempt=%s max_retries=%s",
                item["path_with_namespace"],
                attempt,
                db_max_retries,
                exc_info=True,
            )


def _sync_normalized_project(item):
    """更新 _sync_normalized_project 对应的业务数据，保持现有调用约定。"""
    stats = {
        "synced_projects": 0,
        "created_products": 0,
        "created_modules": 0,
        "created_branches": 0,
        "created_product_modules": 0,
        "created_module_branches": 0,
    }

    product, created = _get_or_create_product(item["product_name"])
    stats["created_products"] += int(created)

    module, created = _get_or_create_module_for_product(product.id, item["module_name"])
    stats["created_modules"] += int(created)

    branch, created = _get_or_create_branch(item["repo_url"], item["default_branch"])
    stats["created_branches"] += int(created)

    created = _ensure_product_module(product.id, module.id)
    stats["created_product_modules"] += int(created)

    created = _ensure_module_branch(module.id, branch.id)
    stats["created_module_branches"] += int(created)
    stats["synced_projects"] += 1

    return stats


def _dispose_current_engine():
    """处理 _dispose_current_engine 对应的业务步骤，并向调用方返回所需结果。"""
    try:
        bind = db.session.get_bind()
        if hasattr(bind, "dispose"):
            bind.dispose()
    except Exception:
        pass


def sync_gitlab_projects_from_config(config):
    """更新 sync_gitlab_projects_from_config 对应的业务数据，保持现有调用约定。"""
    projects = fetch_gitlab_projects(
        config["GIT_BASE_URL"],
        username=config.get("GIT_USER"),
        password=config.get("GIT_PASSWORD"),
        private_token=config.get("GITLAB_PRIVATE_TOKEN"),
        timeout=config.get("GITLAB_API_TIMEOUT", 30),
        membership_only=config.get("GITLAB_PROJECT_MEMBERSHIP_ONLY", False),
        max_retries=config.get("GITLAB_API_MAX_RETRIES", 3),
        retry_delay=config.get("GITLAB_API_RETRY_DELAY", 1),
        page_concurrency=config.get("GITLAB_SYNC_FETCH_CONCURRENCY", 4),
    )
    fetched_count = len(projects)
    logging.info("GitLab 项目拉取阶段完成：fetched=%s", fetched_count)
    skipped_existing = 0
    skip_existing = config.get("GITLAB_SYNC_SKIP_EXISTING_REPOS", True)
    if skip_existing and Product.query.first() is None:
        # 产品表为空说明尚未完成首次注册。即使历史分支记录已存在，也必须全量重建
        # 产品、模块和关联关系，否则启动同步会被“跳过已有仓库”误判成无事可做。
        skip_existing = False
    if skip_existing:
        projects, skipped_existing = filter_projects_missing_repositories(projects)
        logging.info(
            "GitLab 项目增量过滤完成：to_sync=%s skipped_existing=%s",
            len(projects),
            skipped_existing,
        )

    stats = sync_gitlab_projects(projects, db_max_retries=config.get("GITLAB_DB_MAX_RETRIES", 2))
    stats["fetched_projects"] = fetched_count
    stats["existing_repositories_skipped"] = skipped_existing
    return stats


def filter_projects_missing_repositories(projects):
    """处理 filter_projects_missing_repositories 对应的业务步骤，并向调用方返回所需结果。"""
    repo_urls = [project.get("http_url_to_repo") for project in projects if project.get("http_url_to_repo")]
    if not repo_urls:
        return projects, 0

    existing_urls = {
        row[0]
        for row in Branch.query.with_entities(Branch.address)
        .filter(Branch.address.in_(repo_urls))
        .all()
    }
    missing = [project for project in projects if project.get("http_url_to_repo") not in existing_urls]
    return missing, len(projects) - len(missing)


def _get_or_create_product(name):
    """读取并返回 _get_or_create_product 对应的业务数据，保持现有调用约定。"""
    product = Product.query.filter_by(name=name).first()
    if product:
        return product, False

    product = Product(name=name, description=f"GitLab 同步产品: {name}")
    db.session.add(product)
    db.session.flush()
    return product, True


def _get_or_create_module_for_product(product_id, name):
    """读取并返回 _get_or_create_module_for_product 对应的业务数据，保持现有调用约定。"""
    module = (
        db.session.query(Module)
        .join(ProductModule, ProductModule.module_id == Module.id)
        .filter(ProductModule.product_id == product_id, Module.name == name)
        .first()
    )
    if module:
        return module, False

    module = Module(name=name)
    db.session.add(module)
    db.session.flush()
    return module, True


def _get_or_create_branch(repo_url, default_branch):
    """读取并返回 _get_or_create_branch 对应的业务数据，保持现有调用约定。"""
    branch = Branch.query.filter_by(address=repo_url).first()
    if branch:
        if default_branch and not branch.tag_version:
            branch.tag_version = default_branch
        return branch, False

    branch = Branch(address=repo_url, tag_version=default_branch)
    db.session.add(branch)
    db.session.flush()
    return branch, True


def _ensure_product_module(product_id, module_id):
    """校验 _ensure_product_module 对应的业务数据，保持现有调用约定。"""
    relation = ProductModule.query.filter_by(product_id=product_id, module_id=module_id).first()
    if relation:
        return False

    db.session.add(ProductModule(product_id=product_id, module_id=module_id))
    db.session.flush()
    return True


def _ensure_module_branch(module_id, branch_id):
    """校验 _ensure_module_branch 对应的业务数据，保持现有调用约定。"""
    relation = ModuleBranch.query.filter_by(module_id=module_id, branch_id=branch_id).first()
    if relation:
        return False

    db.session.add(ModuleBranch(module_id=module_id, branch_id=branch_id))
    db.session.flush()
    return True
