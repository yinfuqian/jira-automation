"""protocol 模块定义企业微信智能机器人长连接的报文常量与解析辅助。

协议来源：企业微信开发者中心「智能机器人长连接」（document/path/101463）。
长连接由 wss 承载，业务层不再需要 Token/EncodingAESKey 验签解密。
"""
from __future__ import annotations

import re
import uuid


# 上行命令。
CMD_SUBSCRIBE = "aibot_subscribe"
CMD_RESPOND_MSG = "aibot_respond_msg"
CMD_RESPOND_WELCOME_MSG = "aibot_respond_welcome_msg"
CMD_RESPOND_UPDATE_MSG = "aibot_respond_update_msg"
CMD_SEND_MSG = "aibot_send_msg"
CMD_PING = "ping"

# 下行回调命令。
CMD_MSG_CALLBACK = "aibot_msg_callback"
CMD_EVENT_CALLBACK = "aibot_event_callback"

# 事件类型。
EVENT_ENTER_CHAT = "enter_chat"
EVENT_TEMPLATE_CARD = "template_card_event"
EVENT_FEEDBACK = "feedback_event"
EVENT_DISCONNECTED = "disconnected_event"

# 会话类型。
CHAT_TYPE_SINGLE = "single"
CHAT_TYPE_GROUP = "group"

# 群聊里 @机器人 的形态是 content 开头的一串 @昵称，需要剥掉后才能当作问题处理。
# 企业微信在 @ 与正文之间可能插入 U+2005 四分之一空格，这里一并当作空白处理。
_MENTION_PREFIX = re.compile(r"^(?:[\s\u3000\u2005]*@[^\s@\u3000\u2005]+)+[\s\u3000\u2005]*")

# markdown 消息正文的字节上限，超出需要切片发送。
MAX_MARKDOWN_BYTES = 20480


def new_req_id(prefix="cb"):
    """生成请求唯一标识；前缀便于在日志里区分来源。"""
    return f"{prefix}-{uuid.uuid4().hex}"


def new_stream_id(prefix="stream"):
    """生成流式消息标识；同一条流式消息的多次刷新必须复用它。"""
    return f"{prefix}-{uuid.uuid4().hex}"


def build_frame(cmd, body=None, req_id=None):
    """构造上行报文；req_id 缺省时自动生成。"""
    frame = {"cmd": cmd, "headers": {"req_id": req_id or new_req_id()}}
    if body is not None:
        frame["body"] = body
    return frame


def is_callback(payload):
    """判断报文是服务端推送的回调（带 cmd）还是对上行请求的响应。"""
    return isinstance(payload, dict) and bool(payload.get("cmd"))


def strip_mentions(text):
    """去掉群聊消息开头的 @昵称，返回真正的问题文本。"""
    raw = str(text or "")
    return _MENTION_PREFIX.sub("", raw).strip()


def extract_text(body):
    """从文本消息体里提取并清理提问内容，非文本消息返回空串。"""
    payload = body if isinstance(body, dict) else {}
    if str(payload.get("msgtype") or "") != "text":
        return ""
    return strip_mentions((payload.get("text") or {}).get("content"))


def split_utf8(text, limit_bytes=MAX_MARKDOWN_BYTES):
    """按 UTF-8 字节上限切分文本，避免中文被截断成半个字符。"""
    content = str(text or "")
    if not content:
        return [""]
    if len(content.encode("utf-8")) <= limit_bytes:
        return [content]
    chunks = []
    current = []
    size = 0
    for char in content:
        char_size = len(char.encode("utf-8"))
        if size + char_size > limit_bytes:
            chunks.append("".join(current))
            current = [char]
            size = char_size
            continue
        current.append(char)
        size += char_size
    if current:
        chunks.append("".join(current))
    return chunks


def split_for_typewriter(text, chunk_chars, max_chunks):
    """把答案切成打字机效果的若干段。

    优先在换行处断开，保证代码块与列表不被切碎；段数上限用于规避
    「单会话 30 条/分钟」的频率限制，超出的内容并入最后一段。
    """
    content = str(text or "")
    if not content:
        return [""]
    limit = max(1, int(chunk_chars or 400))
    max_parts = max(1, int(max_chunks or 20))
    parts = []
    buffer = ""
    for line in content.splitlines(keepends=True):
        if buffer and len(buffer) + len(line) > limit:
            parts.append(buffer)
            buffer = ""
        buffer += line
        # 单行本身超长时按字符硬切，避免一段过大。
        while len(buffer) > limit:
            parts.append(buffer[:limit])
            buffer = buffer[limit:]
    if buffer:
        parts.append(buffer)
    if len(parts) > max_parts:
        head = parts[: max_parts - 1]
        tail = "".join(parts[max_parts - 1 :])
        parts = head + [tail]
    return parts or [content]
