"""settings 模块从配置文件和 `.env` 加载智能机器人运行配置。

职责边界：
1. 项目、模块分支映射以及网关/仓库/问答参数维护在 `backend/config/chatbot.json`，
   该文件随镜像一起构建，容器内位于 `/app/config/chatbot.json`，不需要宿主机挂载；
2. 只有 BotID 与 Secret 属于敏感凭据，放在 `.env` 的 `CHATBOT_BOT_ID`、`CHATBOT_BOT_SECRET`；
3. GitLab 凭据复用后端已有的 `GIT_BASE_URL`、`GIT_USER`、`GIT_PASSWORD`、`GITLAB_PRIVATE_TOKEN`；
4. Confluence 地址与令牌只来自环境变量，空间、标签和检索阈值来自配置文件。

修改配置文件或 `.env` 后必须重启 `chatbot-gateway` 与 `worker-chatbot`，运行中的进程不会热更新。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


# 配置文件来源说明，用于健康检查与日志。
CONFIG_SOURCE = "配置文件 backend/config/chatbot.json（镜像内置）+ .env 机器人凭证"
# 企业微信智能机器人长连接的固定地址。
DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"
# 网关与 Celery worker 之间的结果通道。
DEFAULT_REPLY_CHANNEL = "chatbot:reply"

# 代码目录内的配置文件：本地为 backend/config/chatbot.json，容器内为 /app/config/chatbot.json。
# 这是默认来源，随镜像发布，不依赖宿主机目录挂载。
CODE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "chatbot.json"
# 兼容旧部署方式：仓库根目录或容器内旧挂载点仍可作为后备来源。
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "chatbot.json"
CONTAINER_CONFIG_PATH = Path("/data/config/chatbot.json")


class ChatbotConfigError(RuntimeError):
    """配置缺失或格式非法时抛出的异常，便于启动时快速失败。"""


def _as_float(value, fallback, minimum=0.0):
    """把配置值转成浮点数，低于 minimum 或非法时回落到默认值。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number >= minimum else fallback


def _as_int(value, fallback, minimum=1):
    """把配置值转成不小于 minimum 的正整数。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return number if number >= minimum else fallback


def _as_bool(value, fallback=True):
    """把配置中的布尔值统一解析；兼容 true/false 与 1/0 写法。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return fallback
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return fallback


def _as_tuple(value):
    """把配置中的字符串或数组归一化为去重元组，保持原顺序。"""
    if value is None:
        return ()
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    result = []
    seen = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return tuple(result)


def _as_text(value, fallback=""):
    """把配置值安全转成字符串并去掉首尾空白。"""
    text = str(value if value is not None else fallback).strip()
    return text or str(fallback or "").strip()


@dataclass(frozen=True)
class ProjectDefinition:
    """一个可被提问的项目，内部包含若干个「模块 → 分支」组合。"""

    key: str
    name: str
    modules: dict = field(default_factory=dict)
    description: str = ""
    product_key: str = ""
    product_name: str = ""
    # 产品别名，用于从自然语言问题中识别项目，例如“招商证券”“call3.1”。
    aliases: tuple = ()

    def module_items(self):
        """返回按模块名排序的 (模块名, 分支) 列表，保证展示顺序稳定。"""
        return sorted((str(k), str(v)) for k, v in self.modules.items() if str(v or "").strip())


@dataclass(frozen=True)
class ProductDefinition:
    """一个产品及其项目、别名和模块仓库地址。"""

    key: str
    name: str
    description: str = ""
    aliases: tuple = ()
    projects: tuple = ()
    repo_urls: dict = field(default_factory=dict)
    order: int = 1000
    source_file: str = field(default="", repr=False)

    def indexed_projects(self):
        """返回产品内从 1 开始编号的项目列表。"""
        return [(index, project) for index, project in enumerate(self.projects, start=1)]

    def project_by_index(self, index):
        """按产品内的 1 起始序号查找项目。"""
        try:
            number = int(str(index).strip())
        except (TypeError, ValueError):
            return None
        for position, project in self.indexed_projects():
            if position == number:
                return project
        return None


@dataclass(frozen=True)
class BotSettings:
    """机器人身份凭证，只从环境变量读取。"""

    bot_id: str = ""
    # Secret 不进入 repr，避免异常日志或调试输出意外泄露。
    secret: str = field(default="", repr=False)

    @property
    def configured(self) -> bool:
        """判断凭证是否齐备；缺失时网关不应启动。"""
        return bool(self.bot_id and self.secret)


@dataclass(frozen=True)
class GatewaySettings:
    """长连接网关的运行参数。"""

    ws_url: str = DEFAULT_WS_URL
    heartbeat_seconds: float = 30.0
    reply_channel: str = DEFAULT_REPLY_CHANNEL
    # 收到 disconnected_event 说明新连接把自己踢掉，此时应退避重连，避免互踢抖动。
    kick_backoff_seconds: float = 60.0
    reconnect_min_seconds: float = 1.0
    reconnect_max_seconds: float = 60.0
    # 打字机效果：每次推送的字符数与间隔，受企微 30 条/分钟的限制约束。
    typewriter_chunk_chars: int = 400
    typewriter_interval_ms: int = 2000
    typewriter_max_chunks: int = 12
    # 首次流式消息发出后，超过该秒数就不再逐段刷新，改为一次性收尾。
    stream_soft_deadline_seconds: float = 480.0


@dataclass(frozen=True)
class RepoSettings:
    """代码检出相关参数；GitLab 凭据复用后端已有的环境变量。"""

    cache_dir: str = ""
    repo_url_template: str = ""
    module_repo_urls: dict = field(default_factory=dict)
    base_url: str = ""
    username: str = ""
    password: str = field(default="", repr=False)
    private_token: str = field(default="", repr=False)
    tls_verify: bool = True
    clone_timeout_seconds: int = 900
    fetch_concurrency: int = 4
    # 单个仓库允许检出的最大体积（MB），超过时按跳过处理并给出提示。
    max_repo_size_mb: int = 2048
    # 代码缓存新鲜度窗口（秒）：窗口内问答复用缓存，超过后再次拉取；0 表示每次都拉取。
    refresh_interval_seconds: int = 3600


@dataclass(frozen=True)
class AnswerSettings:
    """问答阶段的时间与篇幅限制。"""

    timeout_seconds: int = 900
    # 回答面向产品经理，默认限制在 1200 字以内，避免贴出大段代码与检索结果。
    max_answer_chars: int = 1200
    reasoning_effort: str = "medium"
    # 容器内 codex 不能用依赖 bwrap 的只读沙箱，隔离由容器与只读提示词共同保证。
    sandbox: str = "danger-full-access"


@dataclass(frozen=True)
class FaultAnalysisSettings:
    """故障分析问答的开关、模块范围与知识库背景资料设置。"""

    enabled: bool = True
    # 一次故障分析最多检出多少个模块。
    max_modules: int = 4
    include_knowledge: bool = True
    knowledge_chunks: int = 2


@dataclass(frozen=True)
class ConfluenceSettings:
    """Confluence 连接参数；敏感凭据只来自环境变量。"""

    base_url: str = ""
    username: str = ""
    password: str = field(default="", repr=False)
    token: str = field(default="", repr=False)
    auth_mode: str = "bearer"
    tls_verify: bool = True
    request_timeout_seconds: int = 30

    @property
    def configured(self) -> bool:
        """地址与所选鉴权模式的凭据齐备时才允许同步或在线检索。"""
        if not self.base_url:
            return False
        if str(self.auth_mode or "bearer").strip().lower() == "basic":
            return bool(self.username and self.password)
        return bool(self.token)


@dataclass(frozen=True)
class KnowledgeSettings:
    """Confluence 同步、混合检索和单结论回答参数。"""

    enabled: bool = False
    space_keys: tuple = ()
    # 在线问答默认检索机器人账号可访问的全部空间；设置后才额外收窄在线范围。
    online_search_space_keys: tuple = ()
    labels: tuple = ()
    root_page_ids: tuple = ()
    search_limit: int = 30
    sync_page_limit: int = 5000
    max_pages: int = 20
    online_candidate_limit: int = 100
    max_chunks: int = 6
    max_total_chars: int = 50000
    max_chunk_chars: int = 900
    chunk_overlap_chars: int = 120
    attachment_max_bytes: int = 20 * 1024 * 1024
    attachment_max_chars: int = 120000
    min_score: float = 0.15
    embedding_model: str = ""
    embedding_batch_size: int = 16
    cache_ttl_seconds: int = 300
    sync_interval_seconds: int = 21600


@dataclass(frozen=True)
class ChatbotSettings:
    """一次加载得到的完整机器人配置。"""

    source: str = CONFIG_SOURCE
    bot: BotSettings = field(default_factory=BotSettings)
    gateway: GatewaySettings = field(default_factory=GatewaySettings)
    repos: RepoSettings = field(default_factory=RepoSettings)
    answer: AnswerSettings = field(default_factory=AnswerSettings)
    fault_analysis: FaultAnalysisSettings = field(default_factory=FaultAnalysisSettings)
    confluence: ConfluenceSettings = field(default_factory=ConfluenceSettings)
    knowledge: KnowledgeSettings = field(default_factory=KnowledgeSettings)
    products: tuple = ()
    projects: tuple = ()
    session_ttl_seconds: int = 14400

    def indexed_products(self):
        """返回带序号的产品列表，供第一步选择复用。"""
        return [(index, product) for index, product in enumerate(self.products, start=1)]

    def product_by_key(self, key):
        """按产品标识查找产品，未命中时返回 None。"""
        text = str(key or "").strip()
        for product in self.products:
            if product.key == text:
                return product
        return None

    def product_by_index(self, index):
        """按 1 起始的产品序号查找产品。"""
        try:
            number = int(str(index).strip())
        except (TypeError, ValueError):
            return None
        for position, product in self.indexed_products():
            if position == number:
                return product
        return None

    def projects_for_product(self, product_key):
        """返回某个产品下的项目，保持配置文件顺序。"""
        product = self.product_by_key(product_key)
        return product.projects if product is not None else ()

    def indexed_projects(self):
        """返回全局扁平项目序号，兼容历史调用。"""
        return [(index, project) for index, project in enumerate(self.projects, start=1)]

    def project_by_key(self, key):
        """按项目标识查找项目，未命中时返回 None。"""
        text = str(key or "").strip()
        for project in self.projects:
            if project.key == text:
                return project
        return None

    def project_by_index(self, index):
        """按 1 起始的序号查找项目，越界时返回 None。"""
        try:
            number = int(str(index).strip())
        except (TypeError, ValueError):
            return None
        for position, project in self.indexed_projects():
            if position == number:
                return project
        return None

    def product_menu_text(self) -> str:
        """生成第一步产品选择菜单。"""
        lines = [
            "我可以结合项目代码做技术问答；遇到报错、超时、空白、崩溃、异常等故障时，"
            "会进入故障分析并给出原因、代码定位和验证建议。",
            "",
            "**第一步：请选择产品**，@我 并回复对应序号：",
        ]
        for index, product in self.indexed_products():
            suffix = f"（{len(product.projects)} 个项目）" if product.projects else ""
            detail = f" —— {product.description}" if product.description else ""
            lines.append(f"{index}. **{product.name}**{suffix}{detail}")
        lines.append("")
        lines.append("发送「@我 切换产品」可以重新选择。")
        return "\n".join(lines)

    def project_menu_text(self, product) -> str:
        """生成第二步项目选择菜单，只展示当前产品下的项目。"""
        if product is None:
            return self.product_menu_text()
        lines = [f"**第二步：请选择 {product.name} 下的项目**，@我 并回复对应序号："]
        for index, project in product.indexed_projects():
            suffix = f"（{len(project.module_items())} 个模块）" if project.module_items() else ""
            detail = f" —— {project.description}" if project.description else ""
            lines.append(f"{index}. **{project.name}**{suffix}{detail}")
        lines.extend(["", "发送「@我 切换产品」可以重新选择产品。"])
        return "\n".join(lines)

    def menu_text(self) -> str:
        """兼容旧调用：默认展示第一步产品菜单。"""
        return self.product_menu_text()


def _resolve_config_path(config_path=None) -> Path:
    """按「显式参数 → 代码目录 → 仓库根目录 → 旧挂载点」顺序定位配置文件。"""
    if config_path:
        return Path(config_path).expanduser()
    # 代码目录优先：镜像内置的 /app/config/chatbot.json 不依赖任何宿主机挂载。
    for candidate in (CODE_CONFIG_PATH, DEFAULT_CONFIG_PATH, CONTAINER_CONFIG_PATH):
        if candidate.is_file():
            return candidate
    return CODE_CONFIG_PATH


def _load_document(config_path=None):
    """读取并解析 JSON 配置文件，返回 (配置字典, 文件路径)。"""
    path = _resolve_config_path(config_path)
    if not path.is_file():
        raise ChatbotConfigError(
            f"未找到机器人配置文件 {path}；请维护仓库内的 backend/config/chatbot.json，"
            "该文件会随镜像构建到 /app/config/chatbot.json"
        )
    try:
        # utf-8-sig 兼容 Windows 编辑器写入的 BOM。
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ChatbotConfigError(f"读取机器人配置失败：{path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ChatbotConfigError(f"机器人配置不是合法 JSON：{path} 第 {exc.lineno} 行：{exc.msg}") from exc
    if not isinstance(document, dict):
        raise ChatbotConfigError(f"机器人配置顶层必须是 JSON 对象：{path}")
    return document, path


def _section(document, name):
    """读取配置中的对象段落；缺省时返回空字典。"""
    value = document.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ChatbotConfigError(f"配置项 {name} 必须是 JSON 对象")
    return value


def _mapping(value, name):
    """把 JSON 对象规范成字符串键值对，空值条目直接跳过。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ChatbotConfigError(f"配置项 {name} 必须是 JSON 对象")
    result = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key or "").strip()
        item = str(raw_value or "").strip()
        if key and item:
            result[key] = item
    return result


def _parse_projects(document, product_key="", product_name="", product_aliases=()):
    """解析历史单文件配置中的项目列表。"""
    raw_projects = document.get("projects")
    if raw_projects is None:
        return ()
    return _parse_project_list(
        raw_projects,
        label="projects",
        product_key=product_key,
        product_name=product_name,
        product_aliases=product_aliases,
    )


def _parse_project_list(raw_projects, label, product_key="", product_name="", product_aliases=()):
    """解析一个产品或历史配置中的项目数组。"""
    if not isinstance(raw_projects, list):
        raise ChatbotConfigError(f"配置项 {label} 必须是数组")

    projects = []
    for index, item in enumerate(raw_projects, start=1):
        if not isinstance(item, dict):
            raise ChatbotConfigError(f"{label} 第 {index} 项必须是 JSON 对象")
        key = _as_text(item.get("key"))
        short_name = _as_text(item.get("name"))
        if not key or not short_name:
            raise ChatbotConfigError(f"{label} 第 {index} 项必须同时配置 key 与 name")
        name = short_name
        if product_name and not short_name.startswith(product_name):
            name = f"{product_name} {short_name}"
        aliases = tuple(dict.fromkeys((*_as_tuple(product_aliases), *_as_tuple(item.get("aliases")))))
        projects.append(
            ProjectDefinition(
                key=key,
                name=name,
                modules=_mapping(item.get("modules"), f"{label}[{index}].modules"),
                description=_as_text(item.get("description")),
                product_key=product_key,
                product_name=product_name,
                aliases=aliases,
            )
        )
    return tuple(projects)


def _load_product_documents(config_path):
    """读取配置文件同级 products 目录中的产品文件。"""
    product_dir = Path(config_path).resolve().parent / "products"
    if not product_dir.is_dir():
        return (), product_dir
    documents = []
    for path in sorted(product_dir.glob("*.json"), key=lambda item: item.name.lower()):
        try:
            document = json.loads(path.read_text(encoding="utf-8-sig"))
        except OSError as exc:
            raise ChatbotConfigError(f"读取产品配置失败：{path}：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise ChatbotConfigError(
                f"产品配置不是合法 JSON：{path} 第 {exc.lineno} 行：{exc.msg}"
            ) from exc
        if not isinstance(document, dict):
            raise ChatbotConfigError(f"产品配置顶层必须是 JSON 对象：{path}")
        documents.append((document, path))
    return tuple(documents), product_dir


def _parse_products(product_documents):
    """解析产品文件，校验全局唯一 key，并返回产品与扁平项目列表。"""
    parsed = []
    product_keys = set()
    project_keys = set()
    for document, path in product_documents:
        key = _as_text(document.get("key"))
        name = _as_text(document.get("name"))
        if not key or not name:
            raise ChatbotConfigError(f"产品配置必须同时配置 key 与 name：{path}")
        if key in product_keys:
            raise ChatbotConfigError(f"产品 key 重复：{key}")
        product_keys.add(key)
        aliases = _as_tuple(document.get("aliases"))
        projects = _parse_project_list(
            document.get("projects") or [],
            label=f"{path.name}:projects",
            product_key=key,
            product_name=name,
            product_aliases=aliases,
        )
        for project in projects:
            if project.key in project_keys:
                raise ChatbotConfigError(f"项目 key 重复：{project.key}")
            project_keys.add(project.key)
        repo_section = _section(document, "repos")
        repo_urls = _mapping(
            repo_section.get("module_repo_urls", repo_section),
            f"{path.name}:repos",
        )
        parsed.append(
            ProductDefinition(
                key=key,
                name=name,
                description=_as_text(document.get("description")),
                aliases=aliases,
                projects=projects,
                repo_urls=repo_urls,
                order=_as_int(document.get("order"), 1000, minimum=0),
                source_file=path.name,
            )
        )
    parsed.sort(key=lambda product: (product.order, product.name, product.key))
    products = tuple(parsed)
    projects = tuple(project for product in products for project in product.projects)
    return products, projects


def load_settings(config_path=None) -> ChatbotSettings:
    """从 `backend/config/chatbot.json` 与环境变量构建机器人配置。

    `config_path` 仅供测试或维护脚本显式指定文件，正常业务调用不传参。
    """
    document, path = _load_document(config_path)
    product_documents, product_dir = _load_product_documents(path)
    gateway = _section(document, "gateway")
    repos = _section(document, "repos")
    answer = _section(document, "answer")
    fault_analysis = _section(document, "fault_analysis")
    confluence = _section(document, "confluence")
    knowledge = _section(document, "knowledge")

    products, projects = (
        _parse_products(product_documents)
        if product_documents
        else ((), _parse_projects(document))
    )
    module_repo_urls = _mapping(repos.get("module_repo_urls"), "repos.module_repo_urls")
    for product in products:
        for module, repo_url in product.repo_urls.items():
            existing = module_repo_urls.get(module)
            if existing and existing != repo_url:
                raise ChatbotConfigError(
                    f"模块 {module} 的仓库地址在产品 {product.key} 与其他配置中不一致"
                )
            module_repo_urls[module] = repo_url

    missing_repos = sorted(
        {
            module
            for project in projects
            for module, _branch in project.module_items()
            if module not in module_repo_urls
        }
    )
    if missing_repos:
        raise ChatbotConfigError(f"以下模块缺少显式仓库地址：{', '.join(missing_repos)}")

    return ChatbotSettings(
        source=(
            f"{CONFIG_SOURCE}（{path} + {product_dir}/*.json）"
            if product_documents
            else f"{CONFIG_SOURCE}（{path}）"
        ),
        bot=BotSettings(
            # 凭证只允许来自环境变量，避免误写入可提交的配置文件。
            bot_id=os.getenv("CHATBOT_BOT_ID", "").strip(),
            secret=os.getenv("CHATBOT_BOT_SECRET", "").strip(),
        ),
        gateway=GatewaySettings(
            ws_url=_as_text(gateway.get("ws_url"), DEFAULT_WS_URL) or DEFAULT_WS_URL,
            heartbeat_seconds=_as_float(gateway.get("heartbeat_seconds"), 30.0, minimum=5.0),
            reply_channel=_as_text(gateway.get("reply_channel"), DEFAULT_REPLY_CHANNEL)
            or DEFAULT_REPLY_CHANNEL,
            kick_backoff_seconds=_as_float(gateway.get("kick_backoff_seconds"), 60.0),
            reconnect_min_seconds=_as_float(
                gateway.get("reconnect_min_seconds"), 1.0, minimum=0.5
            ),
            reconnect_max_seconds=_as_float(
                gateway.get("reconnect_max_seconds"), 60.0, minimum=1.0
            ),
            typewriter_chunk_chars=_as_int(gateway.get("typewriter_chunk_chars"), 400),
            typewriter_interval_ms=_as_int(gateway.get("typewriter_interval_ms"), 2000),
            typewriter_max_chunks=_as_int(gateway.get("typewriter_max_chunks"), 12),
            stream_soft_deadline_seconds=_as_float(
                gateway.get("stream_soft_deadline_seconds"), 480.0, minimum=30.0
            ),
        ),
        repos=RepoSettings(
            cache_dir=_as_text(repos.get("cache_dir")),
            repo_url_template=_as_text(repos.get("repo_url_template")),
            module_repo_urls=module_repo_urls,
            # GitLab 凭据复用后端已有变量，避免维护两套账号。
            base_url=os.getenv("GIT_BASE_URL", "").strip(),
            username=os.getenv("GIT_USER", "").strip(),
            password=os.getenv("GIT_PASSWORD", "").strip(),
            private_token=os.getenv("GITLAB_PRIVATE_TOKEN", "").strip(),
            tls_verify=_as_bool(repos.get("tls_verify"), True),
            clone_timeout_seconds=_as_int(repos.get("clone_timeout_seconds"), 900),
            fetch_concurrency=_as_int(repos.get("fetch_concurrency"), 4),
            max_repo_size_mb=_as_int(repos.get("max_repo_size_mb"), 2048),
            refresh_interval_seconds=_as_int(
                repos.get("refresh_interval_seconds"), 3600, minimum=0
            ),
        ),
        answer=AnswerSettings(
            timeout_seconds=_as_int(answer.get("timeout_seconds"), 900),
            max_answer_chars=_as_int(answer.get("max_answer_chars"), 1200),
            reasoning_effort=_as_text(answer.get("reasoning_effort"), "medium") or "medium",
            sandbox=_as_text(answer.get("sandbox"), "danger-full-access") or "danger-full-access",
        ),
        fault_analysis=FaultAnalysisSettings(
            enabled=_as_bool(
                os.getenv("FAULT_ANALYSIS_ENABLED"),
                _as_bool(fault_analysis.get("enabled"), True),
            ),
            max_modules=_as_int(
                os.getenv("FAULT_ANALYSIS_MAX_MODULES") or fault_analysis.get("max_modules"),
                4,
                minimum=1,
            ),
            include_knowledge=_as_bool(
                fault_analysis.get("include_knowledge"), True
            ),
            knowledge_chunks=_as_int(fault_analysis.get("knowledge_chunks"), 2),
        ),
        confluence=ConfluenceSettings(
            base_url=os.getenv("CONFLUENCE_URL", "").strip().rstrip("/"),
            username=os.getenv("CONFLUENCE_USERNAME", "").strip(),
            password=os.getenv("CONFLUENCE_PASSWORD", "").strip(),
            token=os.getenv("CONFLUENCE_TOKEN", "").strip(),
            auth_mode=_as_text(os.getenv("CONFLUENCE_AUTH_MODE"), "bearer").lower(),
            tls_verify=_as_bool(confluence.get("tls_verify"), True),
            request_timeout_seconds=_as_int(confluence.get("request_timeout_seconds"), 30),
        ),
        knowledge=KnowledgeSettings(
            enabled=_as_bool(
                os.getenv("KNOWLEDGE_ENABLED"),
                _as_bool(knowledge.get("enabled"), False),
            ),
            space_keys=_as_tuple(
                os.getenv("KNOWLEDGE_SPACE_KEYS") or knowledge.get("space_keys")
            ),
            online_search_space_keys=_as_tuple(
                os.getenv("KNOWLEDGE_ONLINE_SEARCH_SPACE_KEYS")
                or knowledge.get("online_search_space_keys")
            ),
            labels=_as_tuple(
                os.getenv("KNOWLEDGE_LABELS") or knowledge.get("labels")
            ),
            root_page_ids=_as_tuple(
                os.getenv("KNOWLEDGE_ROOT_PAGE_IDS") or knowledge.get("root_page_ids")
            ),
            search_limit=_as_int(knowledge.get("search_limit"), 30),
            sync_page_limit=_as_int(
                os.getenv("KNOWLEDGE_SYNC_PAGE_LIMIT")
                or knowledge.get("sync_page_limit"),
                5000,
            ),
            max_pages=_as_int(knowledge.get("max_pages"), 20),
            online_candidate_limit=_as_int(
                os.getenv("KNOWLEDGE_ONLINE_CANDIDATE_LIMIT")
                or knowledge.get("online_candidate_limit"),
                100,
            ),
            max_chunks=_as_int(knowledge.get("max_chunks"), 6),
            max_total_chars=_as_int(knowledge.get("max_total_chars"), 50000),
            max_chunk_chars=_as_int(knowledge.get("max_chunk_chars"), 900),
            chunk_overlap_chars=_as_int(knowledge.get("chunk_overlap_chars"), 120, minimum=0),
            attachment_max_bytes=_as_int(
                os.getenv("KNOWLEDGE_ATTACHMENT_MAX_BYTES")
                or knowledge.get("attachment_max_bytes"),
                20 * 1024 * 1024,
            ),
            attachment_max_chars=_as_int(
                os.getenv("KNOWLEDGE_ATTACHMENT_MAX_CHARS")
                or knowledge.get("attachment_max_chars"),
                120000,
            ),
            min_score=_as_float(knowledge.get("min_score"), 0.15),
            embedding_model=_as_text(
                os.getenv("KNOWLEDGE_EMBEDDING_MODEL") or knowledge.get("embedding_model")
            ),
            embedding_batch_size=_as_int(knowledge.get("embedding_batch_size"), 16),
            cache_ttl_seconds=_as_int(knowledge.get("cache_ttl_seconds"), 300),
            sync_interval_seconds=_as_int(
                os.getenv("KNOWLEDGE_SYNC_INTERVAL_SECONDS")
                or knowledge.get("sync_interval_seconds"),
                21600,
            ),
        ),
        products=products,
        projects=projects,
        session_ttl_seconds=_as_int(document.get("session_ttl_seconds"), 14400),
    )
