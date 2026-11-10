"""repos 模块负责把 GitLab 仓库检出到本地，供 Codex 只读分析。

设计要点：
1. 每个「模块 + 分支」维护一份本地缓存仓库，任务之间复用，避免每次重新克隆；
2. 每次问答用 `git worktree` 派生一个一次性工作树，任务结束即删除，
   这样并发任务互不干扰，也不会读到别人执行 fetch 时的中间状态；
3. 令牌只作为本次 git 命令的临时参数传入，origin 始终保存不带凭据的地址，
   不会把 token 写进 .git/config。
"""
from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

import httpx


logger = logging.getLogger(__name__)

# 模块名与分支名会被拼进文件路径，必须限制字符集以防路径穿越。
_SAFE_SEGMENT = re.compile(r"[^0-9A-Za-z._-]+")

# git 报错可能回显带凭据的远端地址，日志与用户提示都必须先脱敏。
_AUTH_IN_URL = re.compile(r"(https?://)[^/@\s]+@", re.IGNORECASE)

# 进程内缓存「模块 → 仓库地址」，避免每次问答都请求 GitLab。
_RESOLVED_URLS = {}

# 缓存新鲜度默认窗口：1 小时内复用已拉取的提交，超过后才在问答时再次 fetch。
DEFAULT_REFRESH_INTERVAL_SECONDS = 3600

# 拉取时间标记写在缓存仓库的 .git 目录内，不污染工作区，也不依赖额外存储。
_FETCH_MARKER_NAME = "chatbot-fetched-at"


class RepoError(RuntimeError):
    """仓库解析、检出或工作树操作失败。"""


def safe_segment(value, fallback="item"):
    """把任意标识转成安全的目录名；全非法字符时回落到 fallback。"""
    text = _SAFE_SEGMENT.sub("-", str(value or "").strip()).strip("-.")
    return text or fallback


def redact_credentials(text):
    """隐藏 URL 中的账号密码或令牌，避免随错误信息泄露。"""
    return _AUTH_IN_URL.sub(r"\1***@", str(text or ""))


def cache_root(settings) -> Path:
    """返回缓存根目录：优先配置，其次 REPO_CACHE_DIR，最后落到临时目录。"""
    configured = str(getattr(settings.repos, "cache_dir", "") or "").strip()
    if configured:
        return Path(configured)
    base = str(os.getenv("REPO_CACHE_DIR") or "").strip()
    if base:
        return Path(base) / "chatbot"
    return Path("/tmp/jira-automation-repos") / "chatbot"


def _basic_auth_header(username, password):
    """构造 GitLab Basic 认证头。"""
    raw = f"{username or ''}:{password or ''}".encode("utf-8")
    return f"Basic {base64.b64encode(raw).decode('ascii')}"


def gitlab_auth_headers(settings):
    """按配置生成 GitLab API 认证头，优先使用私有令牌。"""
    if settings.repos.private_token:
        return {"PRIVATE-TOKEN": settings.repos.private_token}
    if settings.repos.username:
        return {"Authorization": _basic_auth_header(settings.repos.username, settings.repos.password)}
    return {}


def build_authenticated_url(repo_url, username, password):
    """把凭据内嵌进 http(s) 地址；非 http 地址或已带凭据时原样返回。"""
    parsed = urlparse(str(repo_url or ""))
    if parsed.scheme not in ("http", "https"):
        return str(repo_url or "")
    if "@" in parsed.netloc:
        return str(repo_url or "")
    user = quote(str(username or ""), safe="")
    secret = quote(str(password or ""), safe="")
    auth = f"{user}:{secret}@" if secret else f"{user}@"
    return urlunparse(parsed._replace(netloc=f"{auth}{parsed.netloc}"))


def resolve_fetch_url(settings, repo_url):
    """生成 git 用的带凭据地址：GitLab 私有令牌优先，账号密码兜底。"""
    # 企业 GitLab 的 HTTP Basic 密码通道可能关闭，但私有令牌仍具备 read_repository
    # 权限；优先使用令牌避免有效配置被失效密码遮蔽。
    if settings.repos.private_token:
        return build_authenticated_url(repo_url, "gitlab-ci-token", settings.repos.private_token)
    if settings.repos.username and settings.repos.password:
        return build_authenticated_url(repo_url, settings.repos.username, settings.repos.password)
    return str(repo_url or "")


def _search_gitlab_project(module, settings):
    """调用 GitLab 搜索接口按模块名定位仓库，返回 http 克隆地址。"""
    base_url = str(settings.repos.base_url or "").strip().rstrip("/")
    if not base_url:
        raise RepoError(f"未配置 GitLab 地址，无法解析模块 {module} 的仓库")
    url = f"{base_url}/api/v4/projects"
    params = {"search": module, "simple": "true", "per_page": 100}
    try:
        response = httpx.get(
            url,
            params=params,
            headers=gitlab_auth_headers(settings),
            timeout=30.0,
            verify=bool(settings.repos.tls_verify),
        )
    except httpx.HTTPError as exc:
        raise RepoError(f"访问 GitLab 搜索接口失败：{exc}") from exc
    if response.status_code >= 400:
        raise RepoError(f"GitLab 搜索接口返回 {response.status_code}：{module}")

    candidates = []
    for item in response.json() or []:
        name = str(item.get("name") or "")
        path = str(item.get("path") or "")
        # 只接受名称完全匹配的仓库，避免 search 返回的子串命中造成串仓。
        if module not in (name, path):
            continue
        # 本服务用账号密码或私有令牌认证，只能走 HTTP(S)；SSH 地址无法注入凭据。
        repo_url = item.get("http_url_to_repo")
        if not repo_url:
            continue
        candidates.append((len(str(item.get("path_with_namespace") or "")), repo_url))
    if not candidates:
        raise RepoError(
            f"GitLab 中找不到名为 {module} 的仓库；请在配置 repos.module_repo_urls 里显式指定地址"
        )
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def resolve_repo_url(module, settings) -> str:
    """解析模块对应的仓库地址：显式映射 → 地址模板 → GitLab 搜索。"""
    name = str(module or "").strip()
    explicit = settings.repos.module_repo_urls.get(name)
    if explicit:
        return explicit
    if name in _RESOLVED_URLS:
        return _RESOLVED_URLS[name]

    template = str(settings.repos.repo_url_template or "").strip()
    if template:
        resolved = template.replace("{module}", name)
    else:
        resolved = _search_gitlab_project(name, settings)
    logger.info("模块 %s 使用仓库：%s", name, redact_credentials(resolved))
    _RESOLVED_URLS[name] = resolved
    return resolved


def _run_git(args, timeout, cwd=None):
    """执行 git 命令；失败时抛出带 stderr 摘要的 RepoError。"""
    try:
        # 服务端没有终端，必须禁用凭据提示，否则私有仓库会一直卡在等待输入。
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "never"
        completed = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RepoError(f"git 命令超时（{timeout}s）：{' '.join(args[:3])}") from exc
    except OSError as exc:
        raise RepoError(f"无法执行 git：{exc}") from exc
    if completed.returncode != 0:
        detail = redact_credentials((completed.stderr or completed.stdout or "").strip())
        raise RepoError(f"git 执行失败（exit={completed.returncode}）：{detail[:400]}")
    return completed.stdout or ""


class RedisLock:
    """基于 Redis 的简单互斥锁，用于串行化同一仓库的 fetch 与建树。"""

    def __init__(self, client, key, ttl_seconds=900, wait_seconds=900, poll_seconds=0.5):
        """记录锁键、持有时长与最长等待时间。"""
        self._client = client
        self._key = key
        self._ttl = max(30, int(ttl_seconds))
        self._wait = max(1, int(wait_seconds))
        self._poll = max(0.1, float(poll_seconds))
        self._token = f"{os.getpid()}-{time.time()}"
        self._held = False

    def acquire(self):
        """轮询获取锁，超时则抛出 RepoError。"""
        if self._client is None:
            self._held = True
            return self
        deadline = time.time() + self._wait
        while time.time() < deadline:
            if self._client.set(self._key, self._token, nx=True, ex=self._ttl):
                self._held = True
                return self
            time.sleep(self._poll)
        raise RepoError(f"等待仓库锁超时：{self._key}")

    def release(self):
        """释放锁；只有仍持有自己的 token 时才删除，避免误删他人锁。"""
        if self._client is None or not self._held:
            return
        try:
            if str(self._client.get(self._key) or "") == self._token:
                self._client.delete(self._key)
        except Exception:  # noqa: BLE001 - 释放失败不应影响任务结果
            logger.warning("释放仓库锁失败：%s", self._key, exc_info=True)
        finally:
            self._held = False

    def __enter__(self):
        """进入上下文时获取锁。"""
        return self.acquire()

    def __exit__(self, exc_type, exc, tb):
        """退出上下文时释放锁。"""
        self.release()
        return False


def _cache_repo_dir(settings, module, branch):
    """计算某个「模块 + 分支」的缓存仓库目录。"""
    return cache_root(settings) / "repos" / safe_segment(module) / safe_segment(branch)


def _init_cache_repo(repo_dir, clean_url, timeout):
    """初始化一个不带工作树的缓存仓库，并写入干净的 origin 地址。"""
    repo_dir.mkdir(parents=True, exist_ok=True)
    _run_git(["git", "init", "--quiet"], timeout=timeout, cwd=str(repo_dir))
    _run_git(["git", "remote", "add", "origin", clean_url], timeout=timeout, cwd=str(repo_dir))


def _fetch_marker_path(repo_dir) -> Path:
    """返回记录最近一次成功拉取时间的标记文件路径。"""
    return Path(repo_dir) / ".git" / _FETCH_MARKER_NAME


def _cache_is_fresh(repo_dir, refresh_interval_seconds) -> bool:
    """判断缓存是否仍在新鲜度窗口内；标记缺失或损坏时按过期处理。"""
    if refresh_interval_seconds <= 0:
        return False
    try:
        fetched_at = float(_fetch_marker_path(repo_dir).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    age = time.time() - fetched_at
    # 时钟回拨导致 age 为负时按过期处理，避免长期不更新代码。
    return 0 <= age < refresh_interval_seconds


def _write_fetch_marker(repo_dir):
    """记录本次拉取成功的时刻；写入失败不影响问答主流程。"""
    marker = _fetch_marker_path(repo_dir)
    temp_path = marker.with_name(f"{marker.name}.tmp")
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(str(time.time()), encoding="utf-8")
        temp_path.replace(marker)
    except OSError:
        logger.debug("写入仓库拉取时间标记失败：%s", marker, exc_info=True)


def _cache_ref_exists(repo_dir, cache_ref) -> bool:
    """检查缓存 ref 是否可用；不存在时说明必须重新拉取。"""
    try:
        _run_git(
            ["git", "rev-parse", "--verify", "--quiet", cache_ref],
            timeout=60,
            cwd=str(repo_dir),
        )
    except RepoError:
        return False
    return True


def prepare_cache_repo(module, branch, settings, lock_client=None):
    """确保缓存仓库可用于本次问答，返回 (目录, 缓存 ref)。

    新鲜度窗口内复用已有缓存，窗口过期或缓存缺失时才拉取分支最新提交。
    """
    repo_url = resolve_repo_url(module, settings)
    repo_dir = _cache_repo_dir(settings, module, branch)
    timeout = int(settings.repos.clone_timeout_seconds or 900)
    fetch_url = resolve_fetch_url(settings, repo_url)
    cache_ref = f"refs/chatbot/cache/{safe_segment(branch)}"
    lock_key = f"chatbot:repo-lock:{safe_segment(module)}:{safe_segment(branch)}"
    refresh_interval = int(
        getattr(settings.repos, "refresh_interval_seconds", DEFAULT_REFRESH_INTERVAL_SECONDS)
    )

    with RedisLock(lock_client, lock_key, ttl_seconds=timeout + 60, wait_seconds=timeout):
        if not (repo_dir / ".git").exists():
            # 目录残留但不是合法仓库时先清理，避免 git init 叠加。
            if repo_dir.exists():
                shutil.rmtree(repo_dir, ignore_errors=True)
            _init_cache_repo(repo_dir, repo_url, timeout)

        # 新鲜度窗口内直接复用缓存 ref，跳过网络拉取；第一个人问答时仍会同步最新代码。
        if _cache_is_fresh(repo_dir, refresh_interval) and _cache_ref_exists(repo_dir, cache_ref):
            logger.info(
                "模块 %s 的 %s 分支缓存未超过 %d 秒，跳过拉取", module, branch, refresh_interval
            )
            return repo_dir, cache_ref

        try:
            _run_git(
                [
                    "git",
                    "fetch",
                    "--depth",
                    "1",
                    "--force",
                    fetch_url,
                    f"+refs/heads/{branch}:{cache_ref}",
                ],
                timeout=timeout,
                cwd=str(repo_dir),
            )
        except RepoError as exc:
            raise RepoError(f"拉取 {module} 的 {branch} 分支失败：{exc}") from exc

        _write_fetch_marker(repo_dir)

    return repo_dir, cache_ref


def create_worktree(repo_dir, cache_ref, worktree_dir, settings):
    """从缓存仓库派生一次性工作树，返回工作树路径。"""
    timeout = int(settings.repos.clone_timeout_seconds or 900)
    target = Path(worktree_dir)
    if target.exists():
        # 同一任务重试时可能残留旧工作树，先移除再建。
        _remove_worktree(repo_dir, target, settings)
    target.parent.mkdir(parents=True, exist_ok=True)
    _run_git(
        ["git", "worktree", "add", "--detach", "--force", str(target), cache_ref],
        timeout=timeout,
        cwd=str(repo_dir),
    )
    return target


def _remove_worktree(repo_dir, target, settings):
    """移除工作树；git 失败时兜底删除目录，并清理 worktree 元数据。"""
    timeout = 120
    try:
        _run_git(
            ["git", "worktree", "remove", "--force", str(target)],
            timeout=timeout,
            cwd=str(repo_dir),
        )
    except RepoError:
        shutil.rmtree(target, ignore_errors=True)
    try:
        _run_git(["git", "worktree", "prune"], timeout=timeout, cwd=str(repo_dir))
    except RepoError:
        logger.debug("清理 worktree 元数据失败：%s", repo_dir, exc_info=True)


def remove_worktree(repo_dir, worktree_dir, settings):
    """对外暴露的工作树清理入口，任何异常都不向上抛。"""
    target = Path(worktree_dir)
    if not target.exists():
        return
    try:
        _remove_worktree(Path(repo_dir), target, settings)
    except Exception:  # noqa: BLE001 - 清理失败不应影响任务结果
        shutil.rmtree(target, ignore_errors=True)


def describe_repo_size(repo_dir):
    """粗略统计仓库体积（MB），用于超限提示。"""
    total = 0
    for root, _dirs, files in os.walk(repo_dir):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total / (1024 * 1024)
