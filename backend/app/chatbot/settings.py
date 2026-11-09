"""settings 模块从配置文件和 `.env` 加载智能机器人运行配置。

职责边界：
1. 项目、模块分支映射以及网关/仓库/问答参数维护在 `backend/config/chatbot.json`，
   该文件随镜像一起构建，容器内位于 `/app/config/chatbot.json`，不需要宿主机挂载；
2. 只有 BotID 与 Secret 属于敏感凭据，放在 `.env` 的 `CHATBOT_BOT_ID`、`CHATBOT_BOT_SECRET`；
3. GitLab 凭据复用后端已有的 `GIT_BASE_URL`、`GIT_USER`、`GIT_PASSWORD`、`GITLAB_PRIVATE_TOKEN`。

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
# 第一机器人的默认队列；知识机器人通过 profile 使用独立队列。
DEFAULT_QUEUE = "chatbot"
# 知识机器人（第二机器人）的 profile 名称。
KNOWLEDGE_PROFILE = "knowledge"

# 代码目录内的配置文件：本地为 backend/config/chatbot.json，容器内为 /app/config/chatbot.json。
# 这是默认来源，随镜像发布，不依赖宿主机目录挂载。
CODE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "chatbot.json"
# 知识机器人配置；仍然随镜像构建，不要求宿主机挂载。
KNOWLEDGE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "knowledge_bot.json"
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


def _as_text(value, fallback=""):
    """把配置值安全转成字符串并去掉首尾空白。"""
    text = str(value if value is not None else fallback).strip()
    return text or str(fallback or "").strip()


def _as_tuple(value):
    """把逗号分隔字符串或数组统一成去重元组，保持原顺序。"""
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


def _profile_name(profile=None):
    """返回当前机器人 profile；空值表示原有代码问答机器人。"""
    return str(profile or os.getenv("CHATBOT_PROFILE") or "").strip().lower()


def _profile_bot_credentials(profile):
    """按 profile 读取各自独立的机器人凭据，绝不跨 profile 回退。"""
    if profile == KNOWLEDGE_PROFILE:
        # 兼容两种命名：优先使用用户确认的短名，同时接受带 BOT_ 的写法。
        bot_id = (
            os.getenv("CHATBOT_KNOWLEDGE_ID", "").strip()
            or os.getenv("CHATBOT_KNOWLEDGE_BOT_ID", "").strip()
        )
        secret = (
            os.getenv("CHATBOT_KNOWLEDGE_SECRET", "").strip()
            or os.getenv("CHATBOT_KNOWLEDGE_BOT_SECRET", "").strip()
        )
        return bot_id, secret
    return (
        os.getenv("CHATBOT_BOT_ID", "").strip(),
        os.getenv("CHATBOT_BOT_SECRET", "").strip(),
    )


def _profile_config_path(profile):
    """返回 profile 对应的默认配置文件路径。"""
    if profile == KNOWLEDGE_PROFILE:
        return KNOWLEDGE_CONFIG_PATH
    return CODE_CONFIG_PATH


@dataclass(frozen=True)
class ProjectDefinition:
    """一个可被提问的项目，内部包含若干个「模块 → 分支」组合。"""

    key: str
    name: str
    modules: dict = field(default_factory=dict)
    description: str = ""

    def module_items(self):
        """返回按模块名排序的 (模块名, 分支) 列表，保证展示顺序稳定。"""
        return sorted((str(k), str(v)) for k, v in self.modules.items() if str(v or "").strip())


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
    # 网关投递任务使用的 Celery 队列；每个机器人必须使用独立队列。
    queue: str = DEFAULT_QUEUE
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
    """故障分析问答的开关与模块范围。"""

    enabled: bool = True
    max_modules: int = 4
    include_knowledge: bool = True
    knowledge_chunks: int = 2


@dataclass(frozen=True)
class InteractionSettings:
    """多轮故障追问与普通问题优先查知识库的交互开关。"""

    fault_dialog_enabled: bool = False
    knowledge_first: bool = False
    fault_dialog_max_turns: int = 4


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
        """地址与所选鉴权模式的凭据齐备时才允许检索。"""
        if not self.base_url:
            return False
        if str(self.auth_mode or "bearer").strip().lower() == "basic":
            return bool(self.username and self.password)
        return bool(self.token)


@dataclass(frozen=True)
class KnowledgeSettings:
    """Confluence 在线检索与单结论回答参数。"""

    enabled: bool = False
    space_keys: tuple = ()
    online_search_space_keys: tuple = ()
    labels: tuple = ()
    root_page_ids: tuple = ()
    search_limit: int = 30
    max_pages: int = 20
    online_candidate_limit: int = 100
    max_chunks: int = 6
    max_total_chars: int = 50000
    max_chunk_chars: int = 900
    chunk_overlap_chars: int = 120
    min_score: float = 0.15
    embedding_model: str = ""
    cache_ttl_seconds: int = 300


@dataclass(frozen=True)
class ChatbotSettings:
    """一次加载得到的完整机器人配置。"""

    source: str = CONFIG_SOURCE
    profile: str = ""
    bot: BotSettings = field(default_factory=BotSettings)
    gateway: GatewaySettings = field(default_factory=GatewaySettings)
    repos: RepoSettings = field(default_factory=RepoSettings)
    answer: AnswerSettings = field(default_factory=AnswerSettings)
    fault_analysis: FaultAnalysisSettings = field(default_factory=FaultAnalysisSettings)
    interaction: InteractionSettings = field(default_factory=InteractionSettings)
    confluence: ConfluenceSettings = field(default_factory=ConfluenceSettings)
    knowledge: KnowledgeSettings = field(default_factory=KnowledgeSettings)
    projects: tuple = ()
    session_ttl_seconds: int = 14400

    def indexed_projects(self):
        """返回带序号的 (序号, 项目) 列表，供文本菜单与序号选择复用。"""
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

    def menu_text(self) -> str:
        """生成项目选择菜单；序号与项目顺序绑定，回答里可直接引用。"""
        # 企业微信只在群聊 @机器人 时才推送消息回调，因此每一步都要提示用户重新 @。
        lines = ["**请先选择要咨询的项目**，@我 并回复对应序号："]
        for index, project in self.indexed_projects():
            suffix = f"（{len(project.module_items())} 个模块）" if project.module_items() else ""
            detail = f" —— {project.description}" if project.description else ""
            lines.append(f"{index}. **{project.name}**{suffix}{detail}")
        lines.append("")
        lines.append("发送「@我 切换项目」可以重新选择。")
        return "\n".join(lines)


def _resolve_config_path(config_path=None, profile="") -> Path:
    """按「显式参数 → 代码目录 → 仓库根目录 → 旧挂载点」顺序定位配置文件。"""
    if config_path:
        return Path(config_path).expanduser()
    # 代码目录优先：镜像内置配置不依赖任何宿主机挂载。
    preferred = _profile_config_path(profile)
    candidates = [preferred]
    if profile != KNOWLEDGE_PROFILE:
        candidates.extend((DEFAULT_CONFIG_PATH, CONTAINER_CONFIG_PATH))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return preferred


def _load_document(config_path=None, profile=""):
    """读取并解析 JSON 配置文件，返回 (配置字典, 文件路径)。"""
    path = _resolve_config_path(config_path, profile=profile)
    if not path.is_file():
        raise ChatbotConfigError(
            f"未找到机器人配置文件 {path}；请维护仓库内的 backend/config/chatbot.json 或 "
            "backend/config/knowledge_bot.json，该文件会随镜像构建到 /app/config/"
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


def _parse_projects(document):
    """解析项目列表并校验 key、名称与模块映射。"""
    raw_projects = document.get("projects")
    if raw_projects is None:
        return ()
    if not isinstance(raw_projects, list):
        raise ChatbotConfigError("配置项 projects 必须是数组")

    projects = []
    seen = set()
    for index, item in enumerate(raw_projects, start=1):
        if not isinstance(item, dict):
            raise ChatbotConfigError(f"projects 第 {index} 项必须是 JSON 对象")
        key = _as_text(item.get("key"))
        name = _as_text(item.get("name"))
        if not key or not name:
            raise ChatbotConfigError(f"projects 第 {index} 项必须同时配置 key 与 name")
        if key in seen:
            raise ChatbotConfigError(f"项目 key 重复：{key}")
        seen.add(key)
        projects.append(
            ProjectDefinition(
                key=key,
                name=name,
                modules=_mapping(item.get("modules"), f"projects[{index}].modules"),
                description=_as_text(item.get("description")),
            )
        )
    return tuple(projects)


def _parse_confluence(document, profile):
    """解析 Confluence 连接参数；凭据只从环境变量读取。"""
    section = _section(document, "confluence")
    base_url = (
        os.getenv("CONFLUENCE_URL", "").strip()
        or os.getenv("CONFLUENCE_BASE_URL", "").strip()
        or _as_text(section.get("base_url"))
    )
    auth_mode = (
        os.getenv("CONFLUENCE_AUTH_MODE", "").strip()
        or _as_text(section.get("auth_mode"), "bearer")
        or "bearer"
    )
    return ConfluenceSettings(
        base_url=base_url,
        username=os.getenv("CONFLUENCE_USERNAME", "").strip()
        or _as_text(section.get("username")),
        password=os.getenv("CONFLUENCE_PASSWORD", "").strip(),
        token=os.getenv("CONFLUENCE_TOKEN", "").strip()
        or os.getenv("CONFLUENCE_API_TOKEN", "").strip(),
        auth_mode=auth_mode,
        tls_verify=_as_bool(os.getenv("CONFLUENCE_TLS_VERIFY"), _as_bool(section.get("tls_verify"), True)),
        request_timeout_seconds=_as_int(
            os.getenv("CONFLUENCE_TIMEOUT_SECONDS"),
            _as_int(section.get("request_timeout_seconds"), 30),
        ),
    )


def _parse_knowledge(document):
    """解析 Confluence 在线检索参数；简单值支持环境变量覆盖。"""
    section = _section(document, "knowledge")
    return KnowledgeSettings(
        enabled=_as_bool(os.getenv("KNOWLEDGE_ENABLED"), _as_bool(section.get("enabled"), False)),
        space_keys=_as_tuple(os.getenv("CONFLUENCE_SPACE_KEYS") or section.get("space_keys")),
        online_search_space_keys=_as_tuple(
            os.getenv("CONFLUENCE_ONLINE_SPACE_KEYS") or section.get("online_search_space_keys")
        ),
        labels=_as_tuple(os.getenv("CONFLUENCE_LABELS") or section.get("labels")),
        root_page_ids=_as_tuple(os.getenv("CONFLUENCE_ROOT_PAGE_IDS") or section.get("root_page_ids")),
        search_limit=_as_int(section.get("search_limit"), 30),
        max_pages=_as_int(section.get("max_pages"), 20),
        online_candidate_limit=_as_int(section.get("online_candidate_limit"), 100),
        max_chunks=_as_int(os.getenv("KNOWLEDGE_MAX_CHUNKS"), _as_int(section.get("max_chunks"), 6)),
        max_total_chars=_as_int(section.get("max_total_chars"), 50000),
        max_chunk_chars=_as_int(section.get("max_chunk_chars"), 900),
        chunk_overlap_chars=_as_int(section.get("chunk_overlap_chars"), 120, minimum=0),
        min_score=_as_float(os.getenv("KNOWLEDGE_MIN_SCORE"), _as_float(section.get("min_score"), 0.15)),
        embedding_model=_as_text(section.get("embedding_model")),
        cache_ttl_seconds=_as_int(section.get("cache_ttl_seconds"), 300, minimum=0),
    )


def _parse_interaction(document):
    """解析多轮故障追问参数。"""
    section = _section(document, "interaction")
    return InteractionSettings(
        fault_dialog_enabled=_as_bool(section.get("fault_dialog_enabled"), False),
        knowledge_first=_as_bool(section.get("knowledge_first"), False),
        fault_dialog_max_turns=_as_int(section.get("fault_dialog_max_turns"), 4),
    )


def _parse_fault_analysis(document):
    """解析故障分析参数。"""
    section = _section(document, "fault_analysis")
    return FaultAnalysisSettings(
        enabled=_as_bool(section.get("enabled"), True),
        max_modules=_as_int(section.get("max_modules"), 4),
        include_knowledge=_as_bool(section.get("include_knowledge"), True),
        knowledge_chunks=_as_int(section.get("knowledge_chunks"), 2),
    )


def load_settings(config_path=None, profile=None) -> ChatbotSettings:
    """从配置文件与环境变量构建机器人配置。

    `profile` 为空时保持原有代码问答机器人的全部行为；传入 `knowledge` 时
    只读取知识机器人自己的凭据、配置、队列和 Redis 命名空间。
    `config_path` 仅供测试或维护脚本显式指定文件，正常业务调用不传参。
    """
    profile = _profile_name(profile)
    document, path = _load_document(config_path, profile=profile)
    gateway = _section(document, "gateway")
    repos = _section(document, "repos")
    answer = _section(document, "answer")
    bot_id, bot_secret = _profile_bot_credentials(profile)
    default_queue = "chatbot-knowledge" if profile == KNOWLEDGE_PROFILE else DEFAULT_QUEUE
    default_channel = (
        "chatbot:knowledge:reply" if profile == KNOWLEDGE_PROFILE else DEFAULT_REPLY_CHANNEL
    )
    queue = _as_text(gateway.get("queue"), default_queue) or default_queue
    if profile == KNOWLEDGE_PROFILE:
        queue = os.getenv("CHATBOT_KNOWLEDGE_QUEUE", "").strip() or queue
    else:
        queue = os.getenv("CHATBOT_QUEUE", "").strip() or queue

    return ChatbotSettings(
        source=f"{CONFIG_SOURCE}（{path}）" if not profile else f"知识机器人配置（{path}）",
        profile=profile,
        bot=BotSettings(
            # 凭证只允许来自环境变量，避免误写入可提交的配置文件。
            bot_id=bot_id,
            secret=bot_secret,
        ),
        gateway=GatewaySettings(
            ws_url=_as_text(gateway.get("ws_url"), DEFAULT_WS_URL) or DEFAULT_WS_URL,
            heartbeat_seconds=_as_float(gateway.get("heartbeat_seconds"), 30.0, minimum=5.0),
            reply_channel=_as_text(gateway.get("reply_channel"), default_channel)
            or default_channel,
            queue=queue,
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
            module_repo_urls=_mapping(repos.get("module_repo_urls"), "repos.module_repo_urls"),
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
        fault_analysis=_parse_fault_analysis(document),
        interaction=_parse_interaction(document),
        confluence=_parse_confluence(document, profile),
        knowledge=_parse_knowledge(document),
        projects=_parse_projects(document),
        session_ttl_seconds=_as_int(document.get("session_ttl_seconds"), 14400),
    )
