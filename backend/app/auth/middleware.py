"""middleware 模块负责本文件相关的业务流程、数据转换与依赖协作。"""
import secrets

from flask import g, jsonify, request


ANONYMOUS_ENDPOINTS = {"auth.login", "auth.request_account", "health.live", "health.ready"}

# 允许用 ?ticket= 签名票据免请求头访问的路径前缀与校验函数，由业务模块在装配时注册。
# 浏览器发起的下载（地址栏、超链接、<a download>）无法自定义请求头，只能靠这条通道兜住。
TICKET_VERIFIERS = {}

# 可用的接口令牌配置：(令牌键, 允许路径键, 调用方标识键, 默认调用方标识, 默认允许路径)。
# 路径配置缺失时回落到默认允许路径，避免令牌意外获得全接口权限。
# SKILL_API_TOKEN 面向外部系统调用技能接口；ANALYSIS_API_TOKEN 面向技能脚本回写故障分析。
API_TOKEN_CONFIGS = (
    ("SKILL_API_TOKEN", "SKILL_API_TOKEN_PATHS", "SKILL_API_USERNAME", "external-api", "/skill"),
    (
        "ANALYSIS_API_TOKEN",
        "ANALYSIS_API_TOKEN_PATHS",
        "ANALYSIS_API_USERNAME",
        "analysis-skill",
        "/analysis,/logfile,/product/get,/module/get,/module/search",
    ),
)


class ExternalApiPrincipal:
    """外部系统通过接口令牌调用时使用的调用方标识。"""

    def __init__(self, username: str):
        """记录外部调用方在审计日志中显示的用户名。"""
        self.username = username


def extract_bearer_token(header_value):
    """解析或提取并返回 extract_bearer_token 对应的业务数据，保持现有调用约定。"""
    scheme, separator, token = str(header_value or "").partition(" ")
    if separator and scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def _authentication_error():
    """处理 _authentication_error 对应的业务步骤，并向调用方返回所需结果。"""
    return jsonify({
        "code": "authentication_required",
        "message": "请先登录或重新登录",
    }), 401


def extract_api_token(headers):
    """从 X-API-Token 或 Authorization 头中提取外部调用令牌。"""
    explicit = str(headers.get("X-API-Token") or "").strip()
    if explicit:
        return explicit
    return extract_bearer_token(headers.get("Authorization"))


def match_api_token(app, path, headers):
    """判断当前请求是否携带了可用于该路径的接口令牌，命中时返回调用方标识。"""
    provided = extract_api_token(headers)
    if not provided:
        return None
    for token_key, paths_key, username_key, fallback_username, default_paths in API_TOKEN_CONFIGS:
        configured = str(app.config.get(token_key) or "")
        if not configured:
            continue
        raw_paths = str(app.config.get(paths_key) or default_paths)
        allowed_paths = [item.strip() for item in raw_paths.split(",") if item.strip()]
        if allowed_paths and not any(str(path or "").startswith(prefix) for prefix in allowed_paths):
            continue
        if secrets.compare_digest(provided, configured):
            return str(app.config.get(username_key) or fallback_username)
    return None


def register_ticket_verifier(path_prefix: str, verifier) -> None:
    """注册某个路径前缀下可用的签名票据校验函数；重复注册时以最后一次为准。"""
    TICKET_VERIFIERS[str(path_prefix)] = verifier


def match_ticket(app, path, ticket):
    """判断请求是否携带了当前路径可用的签名票据，命中时返回调用方标识。"""
    provided = str(ticket or "").strip()
    if not provided:
        return None
    target = str(path or "")
    for prefix, verifier in TICKET_VERIFIERS.items():
        if not target.startswith(prefix):
            continue
        username = verifier(app, target, provided)
        if username:
            return username
    return None


def install_authentication(app, session_service):
    """注册或配置 install_authentication 对应的业务数据，保持现有调用约定。"""
    @app.before_request
    def require_authenticated_session():
        """处理 require_authenticated_session 对应的业务步骤，并向调用方返回所需结果。"""
        if request.method == "OPTIONS" or request.endpoint in ANONYMOUS_ENDPOINTS:
            return None
        if app.testing and app.config.get("AUTH_TEST_BYPASS"):
            return None

        # 外部系统与技能脚本可使用独立接口令牌调用指定接口，避免与登录会话令牌混用。
        principal_username = match_api_token(app, request.path, request.headers)
        if principal_username:
            g.current_user = ExternalApiPrincipal(principal_username)
            g.auth_token = None
            return None

        # 浏览器直接下载交付物时带不了请求头，改用 download_url 里自带的短时效签名票据。
        ticket_username = match_ticket(app, request.path, request.args.get("ticket"))
        if ticket_username:
            g.current_user = ExternalApiPrincipal(ticket_username)
            g.auth_token = None
            return None

        raw_token = extract_bearer_token(request.headers.get("Authorization"))
        user = session_service.authenticate(raw_token)
        if not user:
            return _authentication_error()

        g.current_user = user
        g.auth_token = raw_token
        return None
