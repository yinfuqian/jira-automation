"""session 模块负责把会话状态放进 Redis，供网关与 Celery worker 共享。

网关与 worker 是不同进程，会话（当前选中的项目）、消息去重标记、
流式消息上下文都必须落在 Redis，不能放在进程内存。
"""
from __future__ import annotations

import json
import time


# 会话键：一个用户在一个会话（群或单聊）里选中的项目。
SESSION_KEY = "chatbot:session:{chatid}:{userid}"
# 消息去重键：企业微信在超时或网络抖动时会重推，同一 msgid 只能处理一次。
MSGID_KEY = "chatbot:msgid:{msgid}"
# 流式消息上下文：worker 结束后需要据此回推最终内容。
STREAM_KEY = "chatbot:stream:{task_id}"
# 网关存活标记：健康检查据此判断长连接进程是否还在。
ALIVE_KEY = "chatbot:gateway:alive"


def _text(value):
    """把标识类参数归一化为去空白的字符串。"""
    return str(value or "").strip()


class ChatSessionStore:
    """基于 Redis 的会话存储；所有键都带 TTL，避免无限增长。

    `namespace` 用于隔离多个机器人。默认值 `chatbot` 保持原有键名不变，
    知识机器人传入 `chatbot:knowledge` 后不会读写原机器人的任何键。
    """

    def __init__(self, client, ttl_seconds=14400, dedupe_ttl_seconds=86400, namespace="chatbot"):
        """注入 Redis 客户端、关键 TTL 与键命名空间。"""
        self._client = client
        self._ttl = max(60, int(ttl_seconds or 14400))
        self._dedupe_ttl = max(60, int(dedupe_ttl_seconds or 86400))
        text = str(namespace or "chatbot").strip().strip(":") or "chatbot"
        self._namespace = text

    def _key(self, suffix):
        """拼接带命名空间的 Redis 键。"""
        return f"{self._namespace}:{str(suffix).lstrip(':')}"

    def _session_key(self, chatid, userid):
        """拼出会话键；单聊没有 chatid 时用 userid 兜底。"""
        return self._key(
            "session:{}:{}".format(_text(chatid) or _text(userid), _text(userid))
        )

    def _load_session(self, chatid, userid):
        """读取整个会话对象；缺失或内容损坏时返回空字典。"""
        raw = self._client.get(self._session_key(chatid, userid))
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_session(self, chatid, userid, payload):
        """写回会话对象并刷新 TTL。"""
        self._client.set(
            self._session_key(chatid, userid),
            json.dumps(payload, ensure_ascii=False),
            ex=self._ttl,
        )

    def get_project_key(self, chatid, userid):
        """读取当前会话选中的项目标识，未选择时返回空串。"""
        return _text(self._load_session(chatid, userid).get("project_key"))

    def set_project(self, chatid, userid, project_key):
        """记录当前会话选中的项目，并保留尚未消费的待回答问题。"""
        payload = self._load_session(chatid, userid)
        payload["project_key"] = _text(project_key)
        payload["updated_at"] = time.time()
        self._save_session(chatid, userid, payload)

    def clear_project(self, chatid, userid):
        """清除项目选择，但保留待回答问题，返回该问题供菜单提示。"""
        payload = self._load_session(chatid, userid)
        payload.pop("project_key", None)
        question = _text(payload.get("pending_question"))
        if question:
            payload["updated_at"] = time.time()
            self._save_session(chatid, userid, payload)
        else:
            self._client.delete(self._session_key(chatid, userid))
        return question

    def set_pending_question(self, chatid, userid, question):
        """暂存用户尚未选择项目时提出的问题，选完项目后直接回答。"""
        payload = self._load_session(chatid, userid)
        payload["pending_question"] = _text(question)
        payload["updated_at"] = time.time()
        self._save_session(chatid, userid, payload)

    def pop_pending_question(self, chatid, userid):
        """取出并清除待回答问题；没有时返回空串。"""
        payload = self._load_session(chatid, userid)
        question = _text(payload.pop("pending_question", ""))
        if question:
            payload["updated_at"] = time.time()
            self._save_session(chatid, userid, payload)
        return question

    def claim_msgid(self, msgid):
        """抢占消息处理权；返回 True 表示首次处理，False 表示重复推送。"""
        key = self._key(f"msgid:{_text(msgid)}")
        if not _text(msgid):
            return True
        return bool(self._client.set(key, "1", nx=True, ex=self._dedupe_ttl))

    def save_stream(self, task_id, payload):
        """保存一条流式消息的上下文，供 worker 结束后回推。"""
        self._client.set(
            self._key(f"stream:{_text(task_id)}"),
            json.dumps(payload, ensure_ascii=False),
            ex=self._ttl,
        )

    def load_stream(self, task_id):
        """读取流式消息上下文，缺失时返回空字典。"""
        raw = self._client.get(self._key(f"stream:{_text(task_id)}"))
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def drop_stream(self, task_id):
        """删除流式消息上下文。"""
        self._client.delete(self._key(f"stream:{_text(task_id)}"))

    def mark_alive(self, ttl_seconds=90):
        """写入网关心跳；健康检查读取该键判断长连接进程是否存活。"""
        self._client.set(self._key("gateway:alive"), str(time.time()), ex=max(10, int(ttl_seconds)))

    def alive_at(self):
        """返回网关最近一次心跳时间戳，未上报时返回 0。"""
        raw = self._client.get(self._key("gateway:alive"))
        try:
            return float(raw or 0)
        except (TypeError, ValueError):
            return 0.0

    def get_dialog(self, chatid, userid):
        """读取当前会话的多轮故障追问状态，未开始时返回空字典。"""
        dialog = self._load_session(chatid, userid).get("dialog")
        return dict(dialog) if isinstance(dialog, dict) else {}

    def set_dialog(self, chatid, userid, dialog):
        """保存多轮故障追问状态，并刷新会话 TTL。"""
        payload = self._load_session(chatid, userid)
        payload["dialog"] = dict(dialog or {})
        payload["updated_at"] = time.time()
        self._save_session(chatid, userid, payload)

    def clear_dialog(self, chatid, userid):
        """清除多轮故障追问状态，但保留项目选择等其它会话字段。"""
        payload = self._load_session(chatid, userid)
        payload.pop("dialog", None)
        if payload:
            payload["updated_at"] = time.time()
            self._save_session(chatid, userid, payload)
        else:
            self._client.delete(self._session_key(chatid, userid))

    def reset(self, chatid, userid):
        """删除整个会话，包含项目选择、待回答问题与故障追问状态。"""
        self._client.delete(self._session_key(chatid, userid))
