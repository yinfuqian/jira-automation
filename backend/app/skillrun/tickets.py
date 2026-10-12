"""tickets 模块负责签发与校验技能交付物的短时效下载票据。

浏览器地址栏、超链接与 ``<a download>`` 都无法附加 ``X-API-Token`` 等请求头，
所以任务详情里的 ``download_url`` 随身携带一枚 HMAC 签名票据（``?ticket=...``）：
服务端先确认签名有效且未过期，再确认票据作用域与「任务 + 文件名」完全一致，才放行下载。

设计取舍：

- 票据由密钥签名后自包含，不落库、不依赖 Redis，容器重启或水平扩展都不影响校验；
- 默认 30 分钟过期，不做一次性失效（一次性需要额外的状态存储，收益不足以抵消复杂度）；
- 能读到任务详情的调用方本来就有权下载交付物，下发票据不扩大权限面，泄露影响面也仅限于
  「某个任务下的某个文件」，且很快过期。
"""
from __future__ import annotations

import re

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


# 签名盐值：与其他签名用途隔离，避免不同场景的票据互相通用。
TICKET_SALT = "skill-artifact-download"
# 票据有效期默认值（秒）：覆盖「查详情 → 点下载」的正常间隔，又不会长期留在浏览器历史里。
DEFAULT_TICKET_TTL = 1800
# 交付物下载路径，与路由 /skill/task/<task_id>/artifacts/<path:filename> 对应。
# Werkzeug 会先把路径里的百分号编码解码，所以这里按解码后的字面量匹配。
ARTIFACT_PATH_PATTERN = re.compile(r"^/skill/task/(?P<task_id>[^/]+)/artifacts/(?P<filename>[^/]+)$")


def _serializer(secret: str) -> URLSafeTimedSerializer:
    """按签名密钥构造票据序列化器。"""
    return URLSafeTimedSerializer(str(secret), salt=TICKET_SALT)


def _scope(task_id, filename) -> dict:
    """把票据作用域收敛为「任务 + 文件名」两个字段。"""
    return {"t": str(task_id or ""), "f": str(filename or "")}


def resolve_download_scope(path: str):
    """从请求路径解出 ``(task_id, filename)``；不是交付物下载路径时返回 ``None``。"""
    matched = ARTIFACT_PATH_PATTERN.match(str(path or ""))
    if not matched:
        return None
    return matched.group("task_id"), matched.group("filename")


def issue_ticket(secret: str, task_id: str, filename: str) -> str:
    """为指定任务的指定交付物签发下载票据。"""
    return _serializer(secret).dumps(_scope(task_id, filename))


def verify_ticket(secret: str, ticket: str, task_id: str, filename: str, ttl_seconds: int) -> bool:
    """校验票据的签名、有效期与作用域，任何一项不符都返回 ``False``。"""
    if not secret or not ticket:
        return False
    try:
        payload = _serializer(secret).loads(ticket, max_age=int(ttl_seconds))
    except (BadSignature, SignatureExpired):
        return False
    return payload == _scope(task_id, filename)


def ticket_secret(app) -> str:
    """返回票据签名密钥：优先用专用密钥，缺省回退技能接口令牌。"""
    return str(app.config.get("SKILL_ARTIFACT_TICKET_SECRET") or app.config.get("SKILL_API_TOKEN") or "")


def ticket_ttl(app) -> int:
    """返回票据有效期秒数，未配置时用默认值。"""
    try:
        ttl = int(app.config.get("SKILL_ARTIFACT_TICKET_TTL") or DEFAULT_TICKET_TTL)
    except (TypeError, ValueError):
        return DEFAULT_TICKET_TTL
    return ttl if ttl > 0 else DEFAULT_TICKET_TTL


def sign_download_url(app, task_id: str, filename: str):
    """签发交付物下载地址里的票据；没有可用签名密钥时返回 ``None``（此时只能用请求头下载）。"""
    secret = ticket_secret(app)
    if not secret:
        return None
    return issue_ticket(secret, task_id, filename)


def verify_download_ticket(app, path: str, ticket: str):
    """中间件回调：校验交付物下载票据，通过时返回审计用的调用方标识。"""
    scope = resolve_download_scope(path)
    if scope is None:
        return None
    if not verify_ticket(ticket_secret(app), ticket, scope[0], scope[1], ticket_ttl(app)):
        return None
    return str(app.config.get("SKILL_API_USERNAME") or "external-api")
