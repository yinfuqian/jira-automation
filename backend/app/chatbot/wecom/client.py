"""client 模块实现企业微信智能机器人长连接的 WebSocket 客户端。

职责边界：
- 建立并保持 wss 连接、订阅、心跳、断线重连；
- 维护 req_id → Future 的请求响应关联；
- 向上提供「回复流式消息 / 回复 markdown / 主动推送」等方法；
- 把收到的回调交出去，不在这里做业务处理。

注意：同一机器人同一时间只能保持一条有效长连接，新连接会踢掉旧连接，
因此本客户端只应在一个网关进程里运行，不能开多副本。
"""
from __future__ import annotations

import asyncio
import json
import logging

from .protocol import (
    CMD_PING,
    CMD_RESPOND_MSG,
    CMD_RESPOND_WELCOME_MSG,
    CMD_SEND_MSG,
    CMD_SUBSCRIBE,
    build_frame,
    is_callback,
    new_req_id,
    split_utf8,
)


logger = logging.getLogger(__name__)

try:  # pragma: no cover - 缺少依赖时给出可读提示而不是导入期崩溃
    import websockets
except ImportError:  # pragma: no cover
    websockets = None


class WeComConnectionError(RuntimeError):
    """连接建立、订阅或发送失败。"""


class WeComLongConnection:
    """企业微信长连接客户端；内部自带重连与心跳。"""

    def __init__(
        self,
        ws_url: str,
        bot_id: str,
        secret: str,
        on_callback,
        heartbeat_seconds: float = 30.0,
        reconnect_min_seconds: float = 1.0,
        reconnect_max_seconds: float = 60.0,
        kick_backoff_seconds: float = 60.0,
    ):
        """注入连接参数与回调处理函数。"""
        self._ws_url = ws_url
        self._bot_id = bot_id
        self._secret = secret
        self._on_callback = on_callback
        self._heartbeat_seconds = max(5.0, float(heartbeat_seconds or 30.0))
        self._reconnect_min = max(0.5, float(reconnect_min_seconds or 1.0))
        self._reconnect_max = max(self._reconnect_min, float(reconnect_max_seconds or 60.0))
        self._kick_backoff = max(0.0, float(kick_backoff_seconds or 60.0))

        self._ws = None
        self._pending = {}
        self._tasks = set()
        self._stopped = False
        self._kicked = False

    @property
    def connected(self) -> bool:
        """当前是否已建立并完成订阅。"""
        return self._ws is not None

    @staticmethod
    def _ensure_success(response, action):
        """检查服务端回执；errcode 非 0 时抛出可读异常，便于网关降级。"""
        errcode = response.get("errcode", 0)
        if errcode not in (0, None):
            raise WeComConnectionError(
                f"{action}失败：errcode={errcode} errmsg={response.get('errmsg')}"
            )
        return response
    async def run(self):
        """主循环：连接 → 订阅 → 收消息，异常后退避重连。"""
        if websockets is None:
            raise WeComConnectionError("缺少 websockets 依赖，无法建立长连接")
        if not self._bot_id or not self._secret:
            raise WeComConnectionError("未配置 BotID 或 Secret，无法建立长连接")

        backoff = self._reconnect_min
        while not self._stopped:
            kicked = False
            try:
                await self._serve_once()
                backoff = self._reconnect_min
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 任何异常都走重连
                logger.warning("长连接中断：%s", exc)
            finally:
                kicked = self._kicked
                self._kicked = False
                self._ws = None

            if self._stopped:
                break
            # 被新连接踢掉时不要立刻抢回，否则两边会反复互踢。
            delay = self._kick_backoff if kicked else backoff
            logger.info("将在 %.1f 秒后重连长连接（kicked=%s）", delay, kicked)
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, self._reconnect_max)

    async def stop(self):
        """停止主循环并关闭连接。"""
        self._stopped = True
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 - 关闭失败无需处理
                pass
        for task in list(self._tasks):
            task.cancel()

    async def _serve_once(self):
        """建立一次连接并持续处理消息，直到连接结束。"""
        async with websockets.connect(
            self._ws_url,
            ping_interval=None,  # 使用业务层 ping，避免协议层心跳与企微要求冲突
            open_timeout=20,
            close_timeout=5,
            max_size=8 * 1024 * 1024,
        ) as ws:
            self._ws = ws
            # 必须先启动收包循环再发订阅请求：订阅响应也要靠它写入 Future，
            # 否则 request() 会一直等一个没人读取的响应，直到超时。
            receiver = asyncio.create_task(self._receive_loop(ws), name="wecom-receiver")
            heartbeat = None
            try:
                response = await self.request(
                    CMD_SUBSCRIBE, {"bot_id": self._bot_id, "secret": self._secret}
                )
                errcode = response.get("errcode", 0)
                if errcode not in (0, None):
                    raise WeComConnectionError(
                        f"订阅失败：errcode={errcode} errmsg={response.get('errmsg')}"
                    )
                logger.info("长连接订阅成功，开始接收消息")

                heartbeat = asyncio.create_task(self._heartbeat_loop())
                # 收包循环正常退出代表连接已关闭；异常退出则从这里向上抛出并重连。
                await receiver
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                if not receiver.done():
                    receiver.cancel()
                # 取回收包任务的异常，避免 "Task exception was never retrieved"。
                await asyncio.gather(receiver, return_exceptions=True)
                self._fail_pending("连接已关闭")

    async def _receive_loop(self, ws):
        """持续读取服务端报文，直到连接关闭；订阅响应也由这里唤醒。"""
        async for raw in ws:
            self._dispatch(raw)

    def _dispatch(self, raw):
        """解析一帧报文：回调交给业务层，响应用于唤醒等待中的请求。"""
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("收到无法解析的报文：%s", str(raw)[:200])
            return
        if not isinstance(payload, dict):
            return

        if is_callback(payload):
            task = asyncio.create_task(self._safe_callback(payload))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return

        req_id = (payload.get("headers") or {}).get("req_id")
        future = self._pending.pop(req_id, None)
        if future is not None and not future.done():
            future.set_result(payload)

    async def _safe_callback(self, payload):
        """执行业务回调，异常只记录不终止读循环。"""
        try:
            await self._on_callback(payload)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 单条消息处理失败不能拖垮长连接
            logger.exception("处理长连接回调失败：cmd=%s", payload.get("cmd"))

    async def _heartbeat_loop(self):
        """按配置间隔发送 ping；失败时主动断开以触发重连。"""
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            try:
                response = await self.request(CMD_PING, timeout=10.0)
                if response.get("errcode") not in (0, None):
                    logger.warning("心跳返回异常：%s", response)
                    # 心跳被服务端拒绝时必须断开重连，否则会留下一条无心跳的假连接。
                    ws = self._ws
                    if ws is not None:
                        try:
                            await ws.close()
                        except Exception:  # noqa: BLE001 - 关闭失败交给外层重连
                            pass
                    return
            except Exception as exc:  # noqa: BLE001 - 心跳失败说明连接已坏
                logger.warning("心跳发送失败：%s", exc)
                ws = self._ws
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:  # noqa: BLE001 - 关闭失败交给外层重连
                        pass
                return

    def _fail_pending(self, reason):
        """连接结束时把未完成的请求全部置为失败，避免调用方永久等待。"""
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(WeComConnectionError(reason))
        self._pending.clear()

    async def _send(self, frame):
        """发送一帧报文；未连接时抛出异常。"""
        ws = self._ws
        if ws is None:
            raise WeComConnectionError("长连接尚未建立")
        await ws.send(json.dumps(frame, ensure_ascii=False))

    async def request(self, cmd, body=None, req_id=None, timeout=10.0):
        """发送请求并等待同 req_id 的响应。"""
        loop = asyncio.get_running_loop()
        rid = req_id or new_req_id()
        future = loop.create_future()
        self._pending[rid] = future
        try:
            await self._send(build_frame(cmd, body, rid))
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(rid, None)
            raise WeComConnectionError(f"等待 {cmd} 响应超时") from exc
        finally:
            self._pending.pop(rid, None)
    async def send_welcome(self, req_id, content):
        """回复欢迎语；仅适用于进入会话事件，需在 5 秒内发出。"""
        body = {"msgtype": "text", "text": {"content": str(content or "")[:4000]}}
        return self._ensure_success(
            await self.request(CMD_RESPOND_WELCOME_MSG, body, req_id), "发送欢迎语"
        )

    async def send_stream(self, req_id, stream_id, content, finish, feedback_id=None):
        """回复或刷新流式消息；content 为累计全文，企微按覆盖方式展示。"""
        stream = {"id": stream_id, "finish": bool(finish), "content": str(content or "")}
        if feedback_id:
            stream["feedback"] = {"id": feedback_id}
        return self._ensure_success(
            await self.request(CMD_RESPOND_MSG, {"msgtype": "stream", "stream": stream}, req_id),
            "发送流式消息",
        )

    async def send_markdown(self, req_id, content, feedback_id=None):
        """回复 markdown 消息；超长内容按字节切片后顺序发送。"""
        responses = []
        chunks = split_utf8(content)
        for index, chunk in enumerate(chunks):
            markdown = {"content": chunk}
            if feedback_id and index == 0:
                markdown["feedback"] = {"id": feedback_id}
            responses.append(
                self._ensure_success(
                    await self.request(
                        CMD_RESPOND_MSG, {"msgtype": "markdown", "markdown": markdown}, req_id
                    ),
                    "发送 markdown 消息",
                )
            )
        return responses[-1] if responses else {}

    async def push_markdown(self, chatid, content, chat_type=0):
        """主动推送 markdown，用于回调窗口失效或流式消息已结束的场景。"""
        responses = []
        for chunk in split_utf8(content):
            body = {
                "chatid": chatid,
                "chat_type": int(chat_type or 0),
                "msgtype": "markdown",
                "markdown": {"content": chunk},
            }
            responses.append(
                self._ensure_success(
                    await self.request(CMD_SEND_MSG, body), "主动推送 markdown 消息"
                )
            )
        return responses[-1] if responses else {}
