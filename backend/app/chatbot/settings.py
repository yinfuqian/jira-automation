"""settings 模块从配置文件和 `.env` 加载智能机器人运行配置。

职责边界：
1. 项目、模块分支映射以及网关/仓库/问答参数维护在 `config/chatbot.json`；
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
CONFIG_SOURCE = "配置文件 config/chatbot.json + .env 机器人凭证"
# 企业微信智能机器人长连接的固定地址。
DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"
# 网关与 Celery worker 之间的结果通道。
DEFAULT_REPLY_CHANNEL = "chatbot:reply"

# 配置文件的默认位置：仓库根目录，以及容器内的只读挂载点。
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


@dataclass(frozen=True)
class AnswerSettings:
    """问答阶段的时间与篇幅限制。"""

    timeout_seconds: int = 900
    max_answer_chars: int = 12000
    reasoning_effort: str = "medium"
    sandbox: str = "read-only"


@dataclass(frozen=True)
class ChatbotSettings:
    """一次加载得到的完整机器人配置。"""

    source: str = CONFIG_SOURCE
    bot: BotSettings = field(default_factory=BotSettings)
    gateway: GatewaySettings = field(default_factory=GatewaySettings)
    repos: RepoSettings = field(default_factory=RepoSettings)
    answer: AnswerSettings = field(default_factory=AnswerSettings)
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


def _resolve_config_path(config_path=None) -> Path:
    """按「显式参数 → 仓库配置 → 容器挂载」顺序定位配置文件。"""
    if config_path:
        return Path(config_path).expanduser()
    for candidate in (DEFAULT_CONFIG_PATH, CONTAINER_CONFIG_PATH):
        if candidate.is_file():
            return candidate
    return DEFAULT_CONFIG_PATH


def _load_document(config_path=None):
    """读取并解析 JSON 配置文件，返回 (配置字典, 文件路径)。"""
    path = _resolve_config_path(config_path)
    if not path.is_file():
        raise ChatbotConfigError(
            f"未找到机器人配置文件 {path}；请维护 config/chatbot.json，"
            "容器内应挂载到 /data/config/chatbot.json"
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


def load_settings(config_path=None) -> ChatbotSettings:
    """从 `config/chatbot.json` 与环境变量构建机器人配置。

    `config_path` 仅供测试或维护脚本显式指定文件，正常业务调用不传参。
    """
    document, path = _load_document(config_path)
    gateway = _section(document, "gateway")
    repos = _section(document, "repos")
    answer = _section(document, "answer")

    return ChatbotSettings(
        source=f"{CONFIG_SOURCE}（{path}）",
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
        ),
        answer=AnswerSettings(
            timeout_seconds=_as_int(answer.get("timeout_seconds"), 900),
            max_answer_chars=_as_int(answer.get("max_answer_chars"), 12000),
            reasoning_effort=_as_text(answer.get("reasoning_effort"), "medium") or "medium",
            sandbox=_as_text(answer.get("sandbox"), "read-only") or "read-only",
        ),
        projects=_parse_projects(document),
        session_ttl_seconds=_as_int(document.get("session_ttl_seconds"), 14400),
    )
