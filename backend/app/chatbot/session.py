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
# 故障多轮收集状态：槽位、已追问项和原始问题都在这里，worker 不直接读写。
FAULT_DIALOG_FIELD = "fault_dialog"


def _text(value):
    """把标识类参数归一化为去空白的字符串。"""
    return str(value or "").strip()


class ChatSessionStore:
    """基于 Redis 的会话存储；所有键都带 TTL，避免无限增长。"""

    def __init__(self, client, ttl_seconds=14400, dedupe_ttl_seconds=86400):
        """注入 Redis 客户端与关键 TTL。"""
        self._client = client
        self._ttl = max(60, int(ttl_seconds or 14400))
        self._dedupe_ttl = max(60, int(dedupe_ttl_seconds or 86400))

    def _session_key(self, chatid, userid):
        """拼出会话键；单聊没有 chatid 时用 userid 兜底。"""
        return SESSION_KEY.format(chatid=_text(chatid) or _text(userid), userid=_text(userid))

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

    def get_product_key(self, chatid, userid):
        """读取当前会话选中的产品标识，未选择时返回空串。"""
        return _text(self._load_session(chatid, userid).get("product_key"))

    def set_product(self, chatid, userid, product_key):
        """记录当前产品；进入产品选择阶段时清掉旧项目。"""
        payload = self._load_session(chatid, userid)
        payload["product_key"] = _text(product_key)
        payload.pop("project_key", None)
        # 换产品后旧的故障现场不再适用，避免把信息带到新项目。
        payload.pop(FAULT_DIALOG_FIELD, None)
        payload["updated_at"] = time.time()
        self._save_session(chatid, userid, payload)

    def set_project(self, chatid, userid, project_key):
        """记录当前会话选中的项目，并保留尚未消费的待回答问题。"""
        payload = self._load_session(chatid, userid)
        payload["project_key"] = _text(project_key)
        # 新项目必须重新收集故障环境与路径，不能沿用上一个项目的上下文。
        payload.pop(FAULT_DIALOG_FIELD, None)
        payload["updated_at"] = time.time()
        self._save_session(chatid, userid, payload)

    def clear_selection(self, chatid, userid):
        """清除产品、项目与故障上下文，但保留待回答问题供后续继续处理。"""
        payload = self._load_session(chatid, userid)
        payload.pop("product_key", None)
        payload.pop("project_key", None)
        payload.pop(FAULT_DIALOG_FIELD, None)
        question = _text(payload.get("pending_question"))
        if question:
            payload["updated_at"] = time.time()
            self._save_session(chatid, userid, payload)
        else:
            self._client.delete(self._session_key(chatid, userid))
        return question

    def reset(self, chatid, userid):
        """清空整个会话：项目、待回答问题、故障上下文与流式状态之外的缓存。"""
        self._client.delete(self._session_key(chatid, userid))

    def clear_project(self, chatid, userid):
        """兼容旧调用：切换时清除产品与项目两级选择。"""
        return self.clear_selection(chatid, userid)

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

    def get_pending_question(self, chatid, userid):
        """读取尚未消费的待回答问题；用于自动识别项目而不提前清除。"""
        return _text(self._load_session(chatid, userid).get("pending_question"))

    def get_fault_dialog(self, chatid, userid):
        """读取当前故障收集上下文；没有或内容损坏时返回空字典。"""
        value = self._load_session(chatid, userid).get(FAULT_DIALOG_FIELD)
        return value if isinstance(value, dict) else {}

    def set_fault_dialog(self, chatid, userid, dialog):
        """保存故障收集上下文并刷新会话 TTL。"""
        payload = self._load_session(chatid, userid)
        if isinstance(dialog, dict) and dialog:
            payload[FAULT_DIALOG_FIELD] = dialog
        else:
            payload.pop(FAULT_DIALOG_FIELD, None)
        payload["updated_at"] = time.time()
        self._save_session(chatid, userid, payload)

    def clear_fault_dialog(self, chatid, userid):
        """只清除故障收集上下文，不影响产品、项目和待回答问题。"""
        payload = self._load_session(chatid, userid)
        if FAULT_DIALOG_FIELD not in payload:
            return
        payload.pop(FAULT_DIALOG_FIELD, None)
        remaining = {key for key in payload if key != "updated_at"}
        if remaining:
            payload["updated_at"] = time.time()
            self._save_session(chatid, userid, payload)
        else:
            self._client.delete(self._session_key(chatid, userid))

    def claim_msgid(self, msgid):
        """抢占消息处理权；返回 True 表示首次处理，False 表示重复推送。"""
        key = MSGID_KEY.format(msgid=_text(msgid))
        if not _text(msgid):
            return True
        return bool(self._client.set(key, "1", nx=True, ex=self._dedupe_ttl))

    def save_stream(self, task_id, payload):
        """保存一条流式消息的上下文，供 worker 结束后回推。"""
        self._client.set(
            STREAM_KEY.format(task_id=_text(task_id)),
            json.dumps(payload, ensure_ascii=False),
            ex=self._ttl,
        )

    def load_stream(self, task_id):
        """读取流式消息上下文，缺失时返回空字典。"""
        raw = self._client.get(STREAM_KEY.format(task_id=_text(task_id)))
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def drop_stream(self, task_id):
        """删除流式消息上下文。"""
        self._client.delete(STREAM_KEY.format(task_id=_text(task_id)))

    def mark_alive(self, ttl_seconds=90):
        """写入网关心跳；健康检查读取该键判断长连接进程是否存活。"""
        self._client.set(ALIVE_KEY, str(time.time()), ex=max(10, int(ttl_seconds)))

    def alive_at(self):
        """返回网关最近一次心跳时间戳，未上报时返回 0。"""
        raw = self._client.get(ALIVE_KEY)
        try:
            return float(raw or 0)
        except (TypeError, ValueError):
            return 0.0
