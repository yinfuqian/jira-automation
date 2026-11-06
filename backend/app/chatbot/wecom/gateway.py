"""gateway 模块把长连接收到的事件翻译成业务动作。

它承担「收消息 + 发消息」两端：收到的 @消息 只做解析并投递 Celery，
真正的代码检出与模型分析在 worker 里跑；worker 完成后通过 Redis 频道
把结果送回来，由这里按协议推送流式消息。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from ..session import ChatSessionStore
from ..settings import ChatbotConfigError
from . import protocol
from .client import WeComConnectionError, WeComLongConnection


logger = logging.getLogger(__name__)

# 需要重新选择项目的口语化指令。
SWITCH_COMMANDS = {"切换项目", "换项目", "重新选择", "重选", "切换", "重来"}
# 查看使用说明的指令。
HELP_COMMANDS = {"帮助", "help", "怎么用", "使用说明", "/help"}
# 单聊场景的提示语：本机器人只在群里工作。
SINGLE_CHAT_NOTICE = "我目前只在群里提供代码问答，请在群里 @我 使用。"


class ChatbotGateway:
    """长连接网关：负责协议收发与会话路由，不做耗时计算。"""

    def __init__(self, settings, session_store: ChatSessionStore, enqueue, redis_url=""):
        """注入配置、会话存储与任务投递函数。"""
        self._settings = settings
        self._session = session_store
        self._enqueue = enqueue
        self._redis_url = str(redis_url or "").strip()
        self._connection = None
        self._deliver_lock = asyncio.Lock()
        self._started_at = time.time()

    @property
    def settings(self):
        """当前生效的配置。"""
        return self._settings

    def _build_connection(self) -> WeComLongConnection:
        """按当前配置构造长连接客户端。"""
        gateway = self._settings.gateway
        return WeComLongConnection(
            ws_url=gateway.ws_url,
            bot_id=self._settings.bot.bot_id,
            secret=self._settings.bot.secret,
            on_callback=self.on_callback,
            heartbeat_seconds=gateway.heartbeat_seconds,
            reconnect_min_seconds=gateway.reconnect_min_seconds,
            reconnect_max_seconds=gateway.reconnect_max_seconds,
            kick_backoff_seconds=gateway.kick_backoff_seconds,
        )

    async def run(self):
        """启动长连接、结果订阅与心跳上报，三者任一异常都会整体退出。"""
        if not self._settings.bot.configured:
            raise ChatbotConfigError(
                "未配置 CHATBOT_BOT_ID 或 CHATBOT_BOT_SECRET，请检查 .env"
            )
        if not self._settings.projects:
            raise ChatbotConfigError("未配置任何项目，机器人无法回答")

        self._connection = self._build_connection()
        tasks = [asyncio.create_task(self._connection.run(), name="wecom-connection")]
        tasks.append(asyncio.create_task(self._heartbeat_loop(), name="gateway-heartbeat"))
        if self._redis_url:
            tasks.append(asyncio.create_task(self._reply_listener(), name="reply-listener"))
        else:
            logger.warning("未配置 Redis 地址，worker 结果将无法回推")
        logger.info(
            "机器人网关启动：projects=%s ws=%s",
            len(self._settings.projects),
            self._settings.gateway.ws_url,
        )
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
    async def on_callback(self, payload):
        """分发服务端回调：事件与消息两类处理逻辑不同。"""
        cmd = str(payload.get("cmd") or "")
        body = payload.get("body") or {}
        req_id = (payload.get("headers") or {}).get("req_id") or ""
        if cmd == protocol.CMD_EVENT_CALLBACK:
            await self._handle_event(body, req_id)
        elif cmd == protocol.CMD_MSG_CALLBACK:
            await self._handle_message(body, req_id)
        else:
            logger.debug("忽略未知回调命令：%s", cmd)

    async def _handle_event(self, body, req_id):
        """处理进入会话、连接被踢、用户反馈等事件。"""
        event = (body.get("event") or {}).get("eventtype") or ""
        if event == protocol.EVENT_ENTER_CHAT:
            # 欢迎语必须在收到事件后 5 秒内发出，超时就无法再发。
            # enter_chat 只会出现在单聊；本机器人只在群里工作，直接引导用户去群里 @。
            await self._send_welcome(req_id, "您好，我是代码问答助手。\n\n" + SINGLE_CHAT_NOTICE)
            return
        if event == protocol.EVENT_DISCONNECTED:
            # 收到该事件说明有新的连接顶掉了自己，此时应退避而不是立刻抢回。
            logger.warning("长连接被新连接顶替，进入退避重连")
            if self._connection is not None:
                self._connection._kicked = True
            return
        if event == protocol.EVENT_FEEDBACK:
            logger.info("收到用户反馈事件：%s", json.dumps(body, ensure_ascii=False)[:500])
            return
        logger.debug("忽略事件类型：%s", event)

    async def _handle_message(self, body, req_id):
        """处理群聊 @消息：选项目、切换项目或发起一次问答。"""
        settings = self._settings
        chattype = str(body.get("chattype") or "")
        chatid = str(body.get("chatid") or "")
        userid = str((body.get("from") or {}).get("userid") or "")
        msgid = str(body.get("msgid") or "")

        if chattype != protocol.CHAT_TYPE_GROUP:
            await self._reply(req_id, SINGLE_CHAT_NOTICE)
            return

        if not self._session.claim_msgid(msgid):
            logger.info("忽略重复推送的消息：msgid=%s", msgid)
            return

        question = protocol.extract_text(body)
        if not question:
            await self._reply(req_id, "目前只支持文本提问，请直接输入问题。")
            return

        lowered = question.strip().lower()
        if lowered in HELP_COMMANDS:
            await self._reply(req_id, settings.menu_text())
            return

        if question.strip() in SWITCH_COMMANDS:
            pending_question = self._session.clear_project(chatid, userid)
            content = self._pending_menu_text(pending_question) if pending_question else settings.menu_text()
            await self._reply(req_id, content)
            return

        if question.strip().isdigit():
            project = settings.project_by_index(question)
            if project is None:
                await self._reply(req_id, "序号超出范围。\n\n" + settings.menu_text())
                return
            self._session.set_project(chatid, userid, project.key)
            # 用户先提问、后选项目时，直接沿用刚才的问题发起分析，避免重复输入。
            pending_question = self._session.pop_pending_question(chatid, userid)
            if pending_question:
                await self._start_answer(project, pending_question, body, req_id)
                return
            await self._reply(req_id, self._selected_text(project))
            return

        current = settings.project_by_key(self._session.get_project_key(chatid, userid))
        if current is None:
            # 暂存原问题，选完项目后自动接着回答，用户不需要再问一遍。
            self._session.set_pending_question(chatid, userid, question.strip())
            await self._reply(req_id, self._pending_menu_text(question.strip()))
            return

        await self._start_answer(current, question.strip(), body, req_id)

    def _pending_menu_text(self, question) -> str:
        """在项目菜单后附上待回答的问题，提示选完项目会自动开始分析。"""
        preview = question if len(question) <= 120 else question[:120] + "…"
        return (
            f"{self._settings.menu_text()}\n\n"
            "选好项目后我会直接回答下面这个问题，无需重复提问：\n"
            f"> {preview}"
        )

    def _selected_text(self, project) -> str:
        """生成「已选中项目」的提示语。"""
        modules = project.module_items()
        lines = [
            f"已选择项目：**{project.name}**（{len(modules)} 个模块）",
            "",
        ]
        for module, branch in modules:
            lines.append(f"- {module}：`{branch}`")
        lines.extend(
            [
                "",
                "现在可以直接 @我 提问，例如「结果回调的入口在哪个类？」",
                "发送「@我 切换项目」可重新选择。",
            ]
        )
        return "\n".join(lines)
    async def _start_answer(self, project, question, body, req_id):
        """投递一次问答任务，并先推一条「正在分析」的流式占位消息。"""
        chatid = str(body.get("chatid") or "")
        chattype = str(body.get("chattype") or "")
        userid = str((body.get("from") or {}).get("userid") or "")
        task_id = f"cb-{uuid.uuid4().hex}"
        stream_id = protocol.new_stream_id()
        started_at = time.time()
        modules = project.module_items()

        placeholder = (
            f"正在分析 **{project.name}** 的 {len(modules)} 个模块代码…\n\n"
            "首次分析需要拉取仓库，通常 1~3 分钟，请稍候。"
        )
        try:
            await self._connection.send_stream(req_id, stream_id, placeholder, False)
        except Exception as exc:  # noqa: BLE001 - 占位失败不影响后续推送
            logger.warning("发送流式占位消息失败：%s", exc)

        context = {
            "task_id": task_id,
            "req_id": req_id,
            "stream_id": stream_id,
            "chatid": chatid,
            "chattype": chattype,
            "userid": userid,
            "project_key": project.key,
            "project_name": project.name,
            "question": question,
            "started_at": started_at,
        }
        self._session.save_stream(task_id, context)

        payload = dict(context)
        try:
            await asyncio.to_thread(self._enqueue, payload)
        except Exception as exc:  # noqa: BLE001 - 投递失败要立刻告知用户
            logger.exception("问答任务投递失败：task_id=%s", task_id)
            await self._finish_stream(
                req_id, stream_id, f"任务投递失败，请稍后重试。\n\n> {exc}"
            )
            self._session.drop_stream(task_id)

    async def _reply(self, req_id, content):
        """用 markdown 直接回复一条消息。"""
        if not req_id or self._connection is None:
            return
        try:
            await self._connection.send_markdown(req_id, content)
        except Exception as exc:  # noqa: BLE001 - 回复失败只记录，避免抛回读循环
            logger.warning("回复消息失败：%s", exc)

    async def _send_welcome(self, req_id, content):
        """发送欢迎语；失败时退回普通 markdown 回复。"""
        if self._connection is None:
            return
        try:
            await self._connection.send_welcome(req_id, content)
        except Exception as exc:  # noqa: BLE001 - 欢迎语有时效限制，失败降级
            logger.warning("发送欢迎语失败，改用普通回复：%s", exc)
            await self._reply(req_id, content)

    async def _finish_stream(self, req_id, stream_id, content):
        """结束一条流式消息；失败时退回主动推送。"""
        if self._connection is None:
            return False
        try:
            await self._connection.send_stream(req_id, stream_id, content, True)
            return True
        except Exception as exc:  # noqa: BLE001 - 流式窗口可能已过期
            logger.warning("结束流式消息失败：%s", exc)
            return False

    def _answer_text(self, reply) -> str:
        """把 worker 结果转成最终展示文本，失败时给出可操作提示。"""
        answer = str(reply.get("answer") or "").strip()
        if reply.get("ok"):
            return answer or "分析完成，但没有返回可展示的内容。"
        reason = str(reply.get("error") or "未知原因").strip()
        tip = "可以换个问法重试；若持续失败，请把本条消息截图给管理员。"
        if answer:
            return f"{answer}\n\n---\n**分析未完成**：{reason}\n\n{tip}"
        return f"**分析未完成**：{reason}\n\n{tip}"
    async def _deliver(self, reply):
        """把 worker 结果推给用户：正常走打字机式流式刷新，超时改主动推送。"""
        task_id = str(reply.get("task_id") or "")
        # 流式上下文只保留一份，重复投递直接丢弃。
        context = self._session.load_stream(task_id) if task_id else {}
        if task_id and not context:
            logger.info("跳过重复或过期的问答结果：task_id=%s", task_id)
            return

        req_id = str(reply.get("req_id") or context.get("req_id") or "")
        stream_id = str(reply.get("stream_id") or context.get("stream_id") or "")
        chatid = str(reply.get("chatid") or context.get("chatid") or "")
        chattype = str(reply.get("chattype") or context.get("chattype") or "")
        started_at = float(reply.get("started_at") or context.get("started_at") or 0) or time.time()
        content = self._answer_text(reply)
        gateway = self._settings.gateway

        try:
            if not req_id or self._connection is None:
                await self._push(chatid, chattype, content)
                return

            elapsed = time.time() - started_at
            # 流式消息同样受 20 KB 正文上限约束；超长答案改走可分片的主动推送。
            too_long = len(content.encode("utf-8")) > protocol.MAX_MARKDOWN_BYTES
            if stream_id and not too_long and elapsed <= gateway.stream_soft_deadline_seconds:
                await self._typewriter(req_id, stream_id, content, chatid, chattype)
                return

            # 超过软截止时间或正文超限：先结束流式消息，再用主动推送发完整答案。
            if stream_id:
                note = (
                    "答案较长，完整内容见下一条消息。"
                    if too_long
                    else "分析完成，完整结果见下一条消息。"
                )
                await self._finish_stream(req_id, stream_id, note)
            await self._push(chatid, chattype, content)
        finally:
            if task_id:
                self._session.drop_stream(task_id)

    async def _typewriter(self, req_id, stream_id, content, chatid="", chattype=""):
        """按覆盖式流式刷新模拟打字机效果。"""
        gateway = self._settings.gateway
        parts = protocol.split_for_typewriter(
            content, gateway.typewriter_chunk_chars, gateway.typewriter_max_chunks
        )
        interval = max(0.5, gateway.typewriter_interval_ms / 1000.0)
        accumulated = ""
        for index, part in enumerate(parts):
            accumulated += part
            finish = index == len(parts) - 1
            if not await self._finish_or_refresh(req_id, stream_id, accumulated, finish):
                # 流式窗口失效时直接退回主动推送，保证用户一定拿到答案。
                await self._push(chatid, chattype, content)
                return
            if not finish:
                await asyncio.sleep(interval)

    async def _finish_or_refresh(self, req_id, stream_id, content, finish):
        """发送一次流式刷新，返回是否成功。"""
        if self._connection is None:
            return False
        try:
            await self._connection.send_stream(req_id, stream_id, content, finish)
            return True
        except Exception as exc:  # noqa: BLE001 - 区分不了具体错误，统一降级
            logger.warning("流式刷新失败：%s", exc)
            return False

    async def _push(self, chatid, chattype, content):
        """主动推送 markdown；单聊 chat_type=1，群聊 chat_type=2。"""
        if self._connection is None or not chatid:
            logger.warning("缺少会话标识，无法主动推送消息")
            return
        chat_type = 2 if chattype == protocol.CHAT_TYPE_GROUP else 1
        try:
            await self._connection.push_markdown(chatid, content, chat_type)
        except Exception as exc:  # noqa: BLE001 - 推送失败只能记录日志
            logger.warning("主动推送消息失败：%s", exc)
    async def _reply_listener(self):
        """订阅 Redis 结果频道，把 worker 的产出转成用户可见消息。"""
        import redis.asyncio as aioredis

        channel = self._settings.gateway.reply_channel
        client = aioredis.from_url(self._redis_url, decode_responses=True)
        pubsub = client.pubsub()
        await pubsub.subscribe(channel)
        logger.info("已订阅问答结果频道：%s", channel)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    reply = json.loads(message.get("data") or "{}")
                except (TypeError, ValueError):
                    logger.warning("收到无法解析的问答结果")
                    continue
                if not isinstance(reply, dict):
                    continue
                asyncio.create_task(self._deliver(reply))
        finally:
            try:
                await pubsub.close()
            except Exception:  # noqa: BLE001 - 关闭失败忽略
                pass
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - 关闭失败忽略
                pass

    async def _heartbeat_loop(self):
        """定期上报网关心跳，健康检查据此判断长连接进程是否存活。"""
        while True:
            try:
                self._session.mark_alive()
            except Exception:  # noqa: BLE001 - 心跳失败不应中断服务
                logger.warning("上报网关心跳失败", exc_info=True)
            await asyncio.sleep(30)

