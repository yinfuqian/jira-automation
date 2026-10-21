"""config 模块负责本文件相关的业务流程、数据转换与依赖协作。"""
import os
from pathlib import Path
from urllib.parse import quote_plus


def load_dotenv_file(path, override=False, protected_keys=None):
    """读取 .env 文件并写入进程环境变量。

    override=True 时同名变量以文件值为准，但 protected_keys 中的键保留原值：
    它们来自真实进程环境（shell 导出或 docker compose 注入），优先级高于任何 .env 文件。
    """
    env_path = Path(path)
    if not env_path.exists():
        return

    parsed_values = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not value:
            # 空值表示「未配置」：写进 os.environ 会挤掉后加载文件里的有效值
            # （典型场景是根 .env 的 JIRA_TOKEN= 覆盖掉 backend/.env 里的真实令牌）。
            continue
        parsed_values[key] = value

    protected = {str(key) for key in (protected_keys or ())}
    for key, value in parsed_values.items():
        if key in protected:
            continue
        if override or key not in os.environ:
            os.environ[key] = value


def load_local_env():
    """加载本地配置：仓库根 .env 是唯一权威来源，backend/.env 只做本地开发兜底。

    顺序固定为「先 backend/.env，再根 .env 覆盖」：根 .env 里配置的任何值都不会被
    backend/.env 改写，避免出现「配置写着测试站、实际连到生产站」这类静默错配。
    backend/.env 只用于放根 .env 没有的本地开发路径（SKILLS_DIR、SKILL_WORKSPACE_DIR、
    CODEX_HOME 等），其余键应统一写在根 .env。
    """
    backend_dir = Path(__file__).resolve().parents[1]
    project_root = backend_dir.parent
    # 进程环境（docker compose、shell export）优先级最高，任何 .env 都不得覆盖。
    protected = set(os.environ)
    load_dotenv_file(backend_dir / ".env", override=False, protected_keys=protected)
    load_dotenv_file(project_root / ".env", override=True, protected_keys=protected)


load_local_env()


def _build_mysql_uri():
    """构建并返回 _build_mysql_uri 对应的业务数据，保持现有调用约定。"""
    username = os.getenv("MYSQL_USERNAME", "root")
    password = os.getenv("MYSQL_PASSWORD", "")
    host = os.getenv("MYSQL_HOST", "localhost")
    port = os.getenv("MYSQL_PORT", "3306")
    database = os.getenv("MYSQL_DATABASE", "jira_automation")
    return (
        f"mysql+pymysql://{quote_plus(username)}:{quote_plus(password)}"
        f"@{host}:{port}/{database}?charset=utf8mb4&ssl_disabled=true"
    )


def _build_redis_url():
    """构建并返回 _build_redis_url 对应的业务数据，保持现有调用约定。"""
    username = os.getenv("REDIS_USERNAME", "")
    password = os.getenv("REDIS_PASSWORD", "")
    host = os.getenv("REDIS_HOST", "localhost")
    port = os.getenv("REDIS_PORT", "6379")
    database = os.getenv("REDIS_DATABASE", "0")
    if username:
        auth = f"{quote_plus(username)}:{quote_plus(password)}@"
    elif password:
        auth = f":{quote_plus(password)}@"
    else:
        auth = ""
    return f"redis://{auth}{host}:{port}/{database}"


def _resolve_auth_users_file():
    """解析并返回 _resolve_auth_users_file 对应的业务数据，保持现有调用约定。"""
    backend_dir = Path(__file__).resolve().parents[1]
    configured_path = os.getenv("AUTH_USERS_FILE")
    if not configured_path:
        return str(backend_dir.parent / "users.csv")
    users_path = Path(configured_path).expanduser()
    if users_path.is_absolute():
        return configured_path
    return str((backend_dir / users_path).resolve())


# 技能执行（Codex Agent）的内置默认值：与 Dockerfile、docker-compose 中的容器路径保持一致。
# 部署时除密钥 CODEX_API_KEY、JIRA_TOKEN 必须自行填写外，其余配置可直接沿用这些默认值。
# 技能目录与 CODEX_HOME 分离：技能由宿主机 skills/ 只读挂载，CODEX_HOME 只存放 Codex 运行期状态。
DEFAULT_SKILLS_DIR = "/data/skills"
# 代码检出缓存根目录：容器内由 compose 把它映射到 repo-cache 卷，本地可指向任意可写目录。
# 技能与故障分析都通过 REPO_CACHE_DIR 读取它，不再各自硬编码路径。
DEFAULT_REPO_CACHE_DIR = "/tmp/jira-automation-repos"
DEFAULT_SKILL_WORKSPACE_DIR = "/data/skill-workspace"
DEFAULT_SKILL_API_TOKEN = "local-dev-token"
DEFAULT_SKILL_URL_ALLOWED_HOSTS = "jira.in.wezhuiyi.com"
DEFAULT_CODEX_HOME = "/data/codex"
DEFAULT_CODEX_MODEL = "codex/deepseek-flash"
DEFAULT_CODEX_BASE_URL = "https://newapi.in.wezhuiyi.com/v1"
# 流水线 MCP server：jira-code 的热更新阶段通过它调用平台接口（见 devops-mcp-invoker 技能）。
DEFAULT_DEVOPS_MCP_URL = "https://devops.ks1.wezhuiyi.com/mcp"


def _env_or(name: str, default: str) -> str:
    """读取环境变量，未配置或配置为空字符串时返回默认值，避免空值覆盖内置默认。"""
    value = os.getenv(name)
    return value if value else default


def _optional_int(name: str):
    """读取可选的整型环境变量；未配置、空白或非法时返回 None，表示交给调用方按技能自身声明决定。"""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _resolve_codex_base_url():
    """解析 Codex 使用的模型中转地址：优先 CODEX_BASE_URL，其次由 OPENAI_URL 补齐 /v1，最后用内置默认中转。"""
    configured = os.getenv("CODEX_BASE_URL")
    if configured:
        return configured
    fallback = os.getenv("OPENAI_URL", "")
    if not fallback:
        return DEFAULT_CODEX_BASE_URL
    text = fallback.rstrip("/")
    return text if text.endswith("/v1") else f"{text}/v1"


def _resolve_codex_api_key():
    """解析 Codex 使用的模型密钥，未显式配置时复用 OPENAI_KEY。"""
    return os.getenv("CODEX_API_KEY") or os.getenv("OPENAI_KEY", "")


def _resolve_openai_api_key():
    """解析故障分析模型密钥，兼容常见的 OPENAI_API_KEY 与仅配置 CODEX_API_KEY 的部署。"""
    return (
        os.getenv("OPENAI_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("CODEX_API_KEY")
        or ""
    )


class Config:
    """Config 类封装该领域对象的状态、依赖与相关行为。"""
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", str(120 * 1024 * 1024)))
    MAX_LOG_BYTES = int(os.getenv("MAX_LOG_BYTES", str(100 * 1024 * 1024)))
    MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(10 * 1024 * 1024)))
    MAX_IMAGE_COUNT = int(os.getenv("MAX_IMAGE_COUNT", "10"))
    MAX_IMAGE_PIXELS = int(os.getenv("MAX_IMAGE_PIXELS", "40000000"))

    # 图片识别：默认由多模态模型逐张识别（本地 PaddleOCR 通道默认停用）。
    # 只有显式设置 LOCAL_OCR_ENABLED=true 才会先跑本地 OCR；OCR_GPT_FALLBACK_CONFIDENCE
    # 仅在该本地通道被启用且识别置信度偏低时生效。
    OCR_GPT_FALLBACK_CONFIDENCE = os.getenv("OCR_GPT_FALLBACK_CONFIDENCE", "0.6")

    # 存储类型: 'local'（本地存储） | 'minio'（MinIO） | 'nas'（NAS）
    STORAGE_TYPE = os.getenv("STORAGE_TYPE", "local")

    # 本地存储路径
    LOCAL_STORAGE_DIR = os.getenv("LOCAL_STORAGE_DIR", "/data/upload")
    if not os.path.exists(LOCAL_STORAGE_DIR):
        os.makedirs(LOCAL_STORAGE_DIR, exist_ok=True)

    # 代码检出缓存根目录：故障分析克隆仓库、技能清理检出目录都读这一项，避免各处硬编码路径。
    REPO_CACHE_DIR = _env_or("REPO_CACHE_DIR", DEFAULT_REPO_CACHE_DIR)

    # MinIO 配置todo
    MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "")
    MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
    MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
    MINIO_BUCKET_NAME = os.getenv("MINIO_BUCKET_NAME", "logs")

    # NAS 配置 todo
    NAS_STORAGE_PATH = os.getenv("NAS_STORAGE_PATH", "/mnt/nas/logs")  # 挂载的 NAS 目录

    # 日志配置
    LOG_DIR = os.getenv("LOG_DIR", os.path.join(os.path.abspath(os.path.dirname(__file__)), "logs"))
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR, exist_ok=True)

    LOGGING_LEVEL = os.getenv("LOGGING_LEVEL", "DEBUG")
    LOGGING_FORMAT = os.getenv("LOGGING_FORMAT", "%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    LOGGING_FILE = os.getenv("LOGGING_FILE", os.path.join(LOG_DIR, "app.log"))

    # 数据库 & Redis 配置
    MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
    MYSQL_PORT = os.getenv("MYSQL_PORT", "3306")
    MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "jira_automation")
    MYSQL_USERNAME = os.getenv("MYSQL_USERNAME", "root")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
    SQLALCHEMY_DATABASE_URI = os.getenv("SQLALCHEMY_DATABASE_URI", _build_mysql_uri())
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": int(os.getenv("SQLALCHEMY_POOL_RECYCLE", "1800")),
    }
    REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
    REDIS_PORT = os.getenv("REDIS_PORT", "6379")
    REDIS_DATABASE = os.getenv("REDIS_DATABASE", "0")
    REDIS_USERNAME = os.getenv("REDIS_USERNAME", "")
    REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
    REDIS_URL = os.getenv("REDIS_URL", _build_redis_url())
    REDIS_SOCKET_CONNECT_TIMEOUT = int(os.getenv("REDIS_SOCKET_CONNECT_TIMEOUT", "10"))
    REDIS_SOCKET_TIMEOUT = int(os.getenv("REDIS_SOCKET_TIMEOUT", "60"))
    REDIS_HEALTH_CHECK_INTERVAL = int(os.getenv("REDIS_HEALTH_CHECK_INTERVAL", "30"))
    CELERY_BROKER_SOCKET_TIMEOUT = int(os.getenv("CELERY_BROKER_SOCKET_TIMEOUT", "360"))

    # Authentication configuration
    AUTH_USERS_FILE = _resolve_auth_users_file()
    AUTH_LOGIN_MAX_FAILURES = int(os.getenv("AUTH_LOGIN_MAX_FAILURES", "5"))
    AUTH_LOGIN_WINDOW_SECONDS = int(os.getenv("AUTH_LOGIN_WINDOW_SECONDS", "300"))
    ACCOUNT_REQUEST_PROVIDER = os.getenv("ACCOUNT_REQUEST_PROVIDER", "mock")
    ACCOUNT_REQUEST_API_URL = os.getenv("ACCOUNT_REQUEST_API_URL", "")
    ACCOUNT_REQUEST_API_TOKEN = os.getenv("ACCOUNT_REQUEST_API_TOKEN", "")
    ACCOUNT_REQUEST_API_TIMEOUT = int(os.getenv("ACCOUNT_REQUEST_API_TIMEOUT", "10"))

    # Git 用户密码（敏感信息建议用环境变量）
    GIT_BASE_URL = os.getenv("GIT_BASE_URL", "")
    GIT_USER = os.getenv("GIT_USER", "")
    GIT_PASSWORD = os.getenv("GIT_PASSWORD", "")
    GITLAB_PRIVATE_TOKEN = os.getenv("GITLAB_PRIVATE_TOKEN", "")
    GITLAB_PROJECT_MEMBERSHIP_ONLY = os.getenv("GITLAB_PROJECT_MEMBERSHIP_ONLY", "false").lower() == "true"
    GITLAB_API_TIMEOUT = int(os.getenv("GITLAB_API_TIMEOUT", "30"))
    GITLAB_API_MAX_RETRIES = int(os.getenv("GITLAB_API_MAX_RETRIES", "3"))
    GITLAB_API_RETRY_DELAY = int(os.getenv("GITLAB_API_RETRY_DELAY", "1"))
    GITLAB_DB_MAX_RETRIES = int(os.getenv("GITLAB_DB_MAX_RETRIES", "2"))
    GITLAB_SYNC_SKIP_EXISTING_REPOS = os.getenv("GITLAB_SYNC_SKIP_EXISTING_REPOS", "true").lower() == "true"

    # AI 模型与深度推理配置。所有文本和图片分析共用这一组参数。
    OPENAI_KEY = _resolve_openai_api_key()
    OPENAI_URL = os.getenv("OPENAI_URL", "https://api.deepseek.com")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-sol")
    OPENAI_API_STYLE = os.getenv("OPENAI_API_STYLE", "chat").lower()
    OPENAI_REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "high")

    # 技能执行（Codex Agent）配置：技能目录、工作目录、外部调用令牌与 Codex 运行参数。
    SKILLS_DIR = _env_or("SKILLS_DIR", DEFAULT_SKILLS_DIR)
    SKILL_WORKSPACE_DIR = _env_or("SKILL_WORKSPACE_DIR", DEFAULT_SKILL_WORKSPACE_DIR)
    # 外部调用令牌：默认值仅用于本地验证，生产环境必须替换为随机强令牌。
    SKILL_API_TOKEN = _env_or("SKILL_API_TOKEN", DEFAULT_SKILL_API_TOKEN)
    SKILL_API_TOKEN_PATHS = _env_or("SKILL_API_TOKEN_PATHS", "/skill")
    SKILL_API_USERNAME = _env_or("SKILL_API_USERNAME", "external-api")
    SKILL_URL_ALLOWED_HOSTS = _env_or("SKILL_URL_ALLOWED_HOSTS", DEFAULT_SKILL_URL_ALLOWED_HOSTS)
    # 交付物下载票据：浏览器直接打开 download_url 时无法附加请求头，改用短时效 HMAC 签名票据。
    # 签名密钥缺省回退 SKILL_API_TOKEN；生产环境建议单独配置随机值，便于轮换票据而不影响接口调用。
    SKILL_ARTIFACT_TICKET_SECRET = os.getenv("SKILL_ARTIFACT_TICKET_SECRET", "")
    SKILL_ARTIFACT_TICKET_TTL = int(_env_or("SKILL_ARTIFACT_TICKET_TTL", "1800"))
    # 技能访问 Jira 用到的地址与令牌：作为唯一权威来源注入技能子进程（见 skillrun/env_lock.py），
    # 技能脚本不得改用令牌文件或命令行入参。令牌留空时任务直接失败，避免静默回落到令牌文件。
    # Jira 站点地址不给内置默认值（猜错站点会把评论写到别的环境），必须由 .env 显式提供。
    JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "")
    JIRA_TOKEN = os.getenv("JIRA_TOKEN", "")
    # Jira 自动触发：原生 webhook 可用 HMAC 密钥签名；Jira Automation 可直接使用 SKILL_API_TOKEN。
    JIRA_WEBHOOK_SECRET = os.getenv("JIRA_WEBHOOK_SECRET", "")
    JIRA_WEBHOOK_SKILL_ID = _env_or("JIRA_WEBHOOK_SKILL_ID", "jira-gate-bug")
    JIRA_WEBHOOK_EVENTS = _env_or(
        "JIRA_WEBHOOK_EVENTS", "jira:issue_created,issue_created"
    )
    # 空值表示不按问题类型过滤，事件过滤完全交给 Jira 规则；默认只接收缺陷/Bug。
    JIRA_WEBHOOK_ISSUE_TYPES = os.getenv(
        "JIRA_WEBHOOK_ISSUE_TYPES", "缺陷,Bug"
    )
    CODEX_BIN = _env_or("CODEX_BIN", "codex")
    CODEX_HOME = _env_or("CODEX_HOME", DEFAULT_CODEX_HOME)
    CODEX_MODEL = _env_or("CODEX_MODEL", DEFAULT_CODEX_MODEL)
    CODEX_MODEL_PROVIDER = _env_or("CODEX_MODEL_PROVIDER", "skillrun")
    CODEX_BASE_URL = _resolve_codex_base_url()
    CODEX_API_KEY = _resolve_codex_api_key()
    CODEX_API_KEY_ENV = _env_or("CODEX_API_KEY_ENV", "CODEX_SKILL_API_KEY")
    CODEX_WIRE_API = _env_or("CODEX_WIRE_API", "responses")
    CODEX_REASONING_EFFORT = _env_or("CODEX_REASONING_EFFORT", _env_or("OPENAI_REASONING_EFFORT", "high"))
    CODEX_SANDBOX = _env_or("CODEX_SANDBOX", "danger-full-access")
    CODEX_EPHEMERAL = _env_or("CODEX_EPHEMERAL", "true").lower() == "true"
    CODEX_EXTRA_ARGS = os.getenv("CODEX_EXTRA_ARGS", "")
    # 单次技能执行超时秒数：留空表示按技能自身 runtime.json 的 timeout_seconds 决定（未声明时 1800），
    # 显式配置则覆盖全部技能，便于运维统一收紧或放宽。jira-code 声明 7200，jira-defect-gate 声明 3600。
    CODEX_SKILL_TIMEOUT = _optional_int("CODEX_SKILL_TIMEOUT")
# 流水线 MCP：由后端写进任务独立 CODEX_HOME 的 config.toml 供热更新类技能调用；未配置令牌时不注册。
    DEVOPS_MCP_URL = _env_or("DEVOPS_MCP_URL", DEFAULT_DEVOPS_MCP_URL)
    DEVOPS_MCP_TOKEN = os.getenv("DEVOPS_MCP_TOKEN", "")
    # 连续多少次网络错误后提前判定模型/Jira 不可达，避免空转到超时。
    CODEX_NETWORK_RETRY_LIMIT = int(_env_or("CODEX_NETWORK_RETRY_LIMIT", "10"))
    # worker 启动时是否把上一次运行遗留的“执行中”任务标记为失败。
    SKILL_RECOVER_ORPHANS = _env_or("SKILL_RECOVER_ORPHANS", "true").lower() == "true"
    # 技能回写故障分析：worker 内的技能脚本使用内部令牌调用本服务的上传与故障分析接口。
    # 令牌留空表示关闭该内部通道；路径前缀用于限制令牌可访问的接口范围。
    ANALYSIS_API_TOKEN = os.getenv("ANALYSIS_API_TOKEN", "")
    ANALYSIS_API_TOKEN_PATHS = _env_or(
        "ANALYSIS_API_TOKEN_PATHS", "/analysis,/logfile,/product/get,/module/get,/module/search"
    )
    ANALYSIS_API_USERNAME = _env_or("ANALYSIS_API_USERNAME", "analysis-skill")
    ANALYSIS_API_BASE_URL = _env_or("ANALYSIS_API_BASE_URL", "")
    LOG_ERROR_CONTEXT_LINES = int(os.getenv("LOG_ERROR_CONTEXT_LINES", "10"))
    LOG_CONTEXT_MAX_CHARS = int(os.getenv("LOG_CONTEXT_MAX_CHARS", "30000"))
    
    ## 日志格式
    LOG_CODE_PATTERNS = [
        r'([\w\.]+)\(([\w\.]+):(\d+)\)',           # com.zhuiyi.MyService(MyService.java:123)
        r'at\s+([\w\.]+)\(([\w\.]+):(\d+)\)',       # at com.zhuiyi.Handler(Handler.java:88)
        r'\[([\w\.]+)\]\[(\d+)\]',                 # [com.zhuiyi.auth.AuthInterceptor][43]
    ]
    
    ## 支持的文件类型
    SUPPORTED_LANGUAGES = ['.java', '.py', '.go', '.sh', '.bash', '.zsh']  # Java / Python / Go / Shell


    ## 异步配置
    CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', REDIS_URL)  # Redis 作为消息队列
    CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', REDIS_URL)  # Redis 作为结果存储
    CELERY_BROKER_POOL_LIMIT = int(os.getenv("CELERY_BROKER_POOL_LIMIT", "10"))
    CELERY_VISIBILITY_TIMEOUT = int(os.getenv("CELERY_VISIBILITY_TIMEOUT", "7200"))
    CELERY_BROKER_TRANSPORT_OPTIONS = {
        "socket_keepalive": True,
        "socket_connect_timeout": REDIS_SOCKET_CONNECT_TIMEOUT,
        "socket_timeout": CELERY_BROKER_SOCKET_TIMEOUT,
        "retry_on_timeout": True,
        "health_check_interval": REDIS_HEALTH_CHECK_INTERVAL,
        "visibility_timeout": CELERY_VISIBILITY_TIMEOUT,
    }
    CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS = {
        "socket_connect_timeout": REDIS_SOCKET_CONNECT_TIMEOUT,
        "socket_timeout": REDIS_SOCKET_TIMEOUT,
        "retry_on_timeout": True,
        "health_check_interval": REDIS_HEALTH_CHECK_INTERVAL,
    }
