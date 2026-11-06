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

from .. import fault_analysis, fault_dialog
from ..session import ChatSessionStore
from ..settings import ChatbotConfigError
from . import protocol
from .client import WeComConnectionError, WeComLongConnection


logger = logging.getLogger(__name__)

# 需要回到第一步重新选择产品的口语化指令。
SWITCH_COMMANDS = {"切换产品", "换产品", "切换项目", "换项目", "重新选择", "重选", "切换", "重来"}
# 清空当前会话（含未完成的故障追问），避免旧上下文把新问题当成槽位答案。
RESET_COMMANDS = {
    "重置会话",
    "重置",
    "新会话",
    "清空会话",
    "清空上下文",
    "重新开始",
    "从头开始",
}
# 查看使用说明的指令。
HELP_COMMANDS = {"帮助", "help", "怎么用", "使用说明", "/help"}
# 单聊场景的提示语：本机器人只在群里工作。
SINGLE_CHAT_NOTICE = "我目前只在群里提供代码问答和故障分析，请在群里 @我 使用。"


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
            await self._send_welcome(req_id, "您好，我是代码问答与故障分析助手。\n\n" + SINGLE_CHAT_NOTICE)
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

        logger.info(
            "收到群聊消息：msgid=%s chatid=%s userid=%s",
            msgid,
            chatid,
            userid,
        )

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
        selected_product = settings.product_by_key(self._session.get_product_key(chatid, userid))
        current = settings.project_by_key(self._session.get_project_key(chatid, userid))
        interaction = getattr(settings, "interaction", None)
        dialog_enabled = bool(getattr(interaction, "fault_dialog_enabled", False))
        if current is not None and selected_product is None:
            # 兼容旧会话：历史版本只保存 project_key，后续直接补齐产品层级。
            selected_product = settings.product_by_key(current.product_key)
            if selected_product is not None:
                self._session.set_product(chatid, userid, selected_product.key)
                self._session.set_project(chatid, userid, current.key)

        if question.strip() in RESET_COMMANDS:
            self._session.reset(chatid, userid)
            await self._reply(
                req_id,
                "已重置会话。旧的项目选择和未完成的故障追问都已清除，"
                "直接描述新的问题即可。",
            )
            return

        if lowered in HELP_COMMANDS:
            if dialog_enabled:
                await self._reply(req_id, self._help_text())
                return
            menu = (
                settings.project_menu_text(selected_product)
                if selected_product is not None and current is None
                else settings.product_menu_text()
            )
            await self._reply(req_id, menu)
            return

        if question.strip() in SWITCH_COMMANDS:
            pending_question = self._session.clear_selection(chatid, userid)
            if dialog_enabled:
                await self._reply(
                    req_id,
                    "已清除当前产品/项目选择。直接描述问题即可，不需要重新选择；"
                    "需要指定范围时，在问题里带上产品、项目或模块名。",
                )
                return
            content = (
                self._pending_menu_text(pending_question, settings.product_menu_text())
                if pending_question
                else settings.product_menu_text()
            )
            await self._reply(req_id, content)
            return

        # 故障追问必须优先于序号选择与普通问答：用户此时发送的「1」或长段日志
        # 都是对上一个追问的回答，不能再被当成产品/项目序号。
        active_dialog = self._session.get_fault_dialog(chatid, userid)
        if active_dialog:
            if self._is_new_fault_for_other_product(
                active_dialog, question.strip()
            ):
                # 用户没有先说“重置”，但新描述明确落到另一个产品：按新故障处理，
                # 不能把整句话写进上一个故障的操作路径。
                self._session.clear_fault_dialog(chatid, userid)
                await self._begin_fault_dialog(None, question.strip(), body, req_id)
                return
            if self._is_new_document_question(question.strip()):
                # 用户从故障追问切到了「要文档/要方案」，这是一条新问题，
                # 不能把“给我迁移方案”当成上一条故障的日志或路径。
                self._session.clear_fault_dialog(chatid, userid)
                await self._handle_unscoped_message(
                    current, question.strip(), body, req_id
                )
                return
            await self._continue_fault_dialog(
                current, active_dialog, question.strip(), body, req_id
            )
            return

        # 新交互模式完全取消「先选产品/项目」的前置门槛：能从描述识别就直接
        # 使用，识别不到时按缺失信息逐项追问，普通问题则直接查知识库。
        if dialog_enabled:
            await self._handle_unscoped_message(
                current, question.strip(), body, req_id
            )
            return

        if question.strip().isdigit():
            if selected_product is None:
                selected_product = settings.product_by_index(question)
                if selected_product is None:
                    await self._reply(req_id, "产品序号超出范围。\n\n" + settings.product_menu_text())
                    return
                self._session.set_product(chatid, userid, selected_product.key)
                await self._reply(req_id, settings.project_menu_text(selected_product))
                return

            project = selected_product.project_by_index(question)
            if project is None:
                await self._reply(
                    req_id,
                    f"项目序号超出范围。\n\n{settings.project_menu_text(selected_product)}",
                )
                return
            self._session.set_project(chatid, userid, project.key)
            # 用户先提问、后选项目时，直接沿用刚才的问题发起分析，避免重复输入。
            pending_question = self._session.pop_pending_question(chatid, userid)
            if pending_question:
                await self._route_question(project, pending_question, body, req_id)
                return
            await self._reply(req_id, self._selected_text(project))
            return

        if current is not None:
            await self._route_question(current, question.strip(), body, req_id)
            return

        # 严格按「产品 → 项目 → 回答」推进；问题先暂存，选完后自动继续。
        self._session.set_pending_question(chatid, userid, question.strip())
        menu = (
            settings.project_menu_text(selected_product)
            if selected_product is not None
            else settings.product_menu_text()
        )
        await self._reply(req_id, self._pending_menu_text(question.strip(), menu))

    async def _handle_unscoped_message(self, current, question, body, req_id):
        """处理尚未绑定项目的消息，不再弹出产品/项目菜单。

        代码检出只需要「模块 + 分支」：普通问题直接查知识库，故障/代码问题
        进入追问并只索取模块与分支；项目/客户现场仅作为可选的快捷映射。
        """
        settings = self._settings
        chatid = str(body.get("chatid") or "")
        userid = str((body.get("from") or {}).get("userid") or "")
        fault_question = self._is_fault_request(question)
        code_question = fault_analysis.is_code_question(question)
        document_request = bool(
            fault_analysis.is_document_question(question)
            and not fault_analysis.has_fault_evidence(question)
        )
        knowledge_ready = bool(
            getattr(settings.knowledge, "enabled", False)
            and getattr(settings.confluence, "configured", False)
        )
        knowledge_first = bool(
            getattr(getattr(settings, "interaction", None), "knowledge_first", False)
        )

        candidate = fault_analysis.match_project(settings, question)
        product = fault_analysis.match_product(settings, question)
        if (
            product is not None
            and current is not None
            and current.product_key != product.key
        ):
            # 用户明确提到了另一个产品，不能继续沿用会话里的旧项目。
            current = None
            self._session.pop_pending_question(chatid, userid)
        if (
            product is not None
            and current is not None
            and current.product_key == product.key
            and candidate is None
            and (fault_question or code_question)
            and len(product.projects) > 1
        ):
            # 只提产品、没提客户时，不能沿用上次的客户分支；改为按模块+分支追问。
            current = None
            self._session.set_product(chatid, userid, product.key)
            self._session.pop_pending_question(chatid, userid)
        if candidate is not None and (
            current is None or candidate.key != current.key
        ):
            self._remember_project(candidate, chatid, userid)
            self._session.pop_pending_question(chatid, userid)
            await self._route_question(candidate, question, body, req_id)
            return
        if candidate is not None:
            await self._route_question(candidate, question, body, req_id)
            return
        if current is not None:
            # 会话里有项目映射时可以复用其分支；但没有项目也照样能分析，
            # 后续只按模块 + 分支追问，不会回到项目菜单。
            await self._route_question(current, question, body, req_id)
            return
        if product is not None:
            if len(product.projects) == 1:
                default_project = product.projects[0]
                self._remember_project(default_project, chatid, userid)
                self._session.pop_pending_question(chatid, userid)
                await self._route_question(
                    default_project, question, body, req_id
                )
                return
            if (
                document_request
                or (not fault_question and not code_question)
            ) and knowledge_first and knowledge_ready:
                await self._start_answer(
                    None, question, body, req_id, mode="knowledge"
                )
                return
            self._session.set_product(chatid, userid, product.key)
            await self._begin_fault_dialog(
                None,
                question,
                body,
                req_id,
                mode="fault" if fault_question else "code",
            )
            return

        if fault_question:
            await self._begin_fault_dialog(None, question, body, req_id)
            return
        if (
            knowledge_first
            and knowledge_ready
            and (document_request or not code_question)
        ):
            await self._start_answer(None, question, body, req_id, mode="knowledge")
            return
        if code_question:
            await self._begin_fault_dialog(
                None, question, body, req_id, mode="code"
            )
            return

        # 知识库未就绪或未开启优先时，也必须查代码；只索取模块与分支。
        await self._begin_fault_dialog(None, question, body, req_id, mode="code")

    def _remember_project(self, project, chatid, userid):
        """记住自动识别出的项目；后续追问和普通问题可直接复用。"""
        product = self._settings.product_by_key(project.product_key)
        if product is not None:
            self._session.set_product(chatid, userid, product.key)
        self._session.set_project(chatid, userid, project.key)

    def _is_fault_request(self, question):
        """故障请求要排除纯文档/方案类问法，避免误进追问。"""
        return bool(
            fault_analysis.is_fault_analysis_question(question)
            and not (
                fault_analysis.is_document_question(question)
                and not fault_analysis.has_fault_evidence(question)
            )
        )

    def _is_new_document_question(self, question):
        """故障追问期间，判断用户是否已经切换到索要文档/方案。"""
        return bool(
            fault_analysis.is_document_question(question)
            and not fault_analysis.has_fault_evidence(question)
            and not self._is_fault_request(question)
        )

    def _is_new_fault_for_other_product(self, dialog, question):
        """判断这轮回复是否其实是另一个产品的新故障描述。"""
        if not fault_analysis.is_fault_analysis_question(question):
            return False
        product = fault_analysis.match_product(self._settings, question)
        if product is None:
            return False
        dialog_product = str(dialog.get("product_key") or "").strip()
        if not dialog_product:
            dialog_project = self._settings.project_by_key(
                dialog.get("project_key")
            )
            dialog_product = (
                dialog_project.product_key if dialog_project is not None else ""
            )
        return bool(dialog_product) and dialog_product != product.key

    def _resolve_dialog_project(self, dialog):
        """从原始描述里解析项目；项目仅用于复用预配置分支，不是必需项。"""
        existing = self._settings.project_by_key(dialog.get("project_key"))
        if existing is not None:
            return existing
        return fault_analysis.match_project(
            self._settings, fault_dialog.analysis_query(dialog)
        )

    def _save_fault_dialog(self, project, dialog, chatid, userid):
        """先更新项目选择，再写回故障上下文，避免被 set_project 清掉。"""
        if project is not None:
            self._remember_project(project, chatid, userid)
        self._session.set_fault_dialog(chatid, userid, dialog)

    def _help_text(self) -> str:
        """新交互模式的帮助：直接描述问题，不要求项目或客户现场。"""
        return (
            "直接 @我 描述问题即可，不需要先选产品或客户现场。\n\n"
            "- 普通问题：查询 Confluence 已同步资料，并给出来源页面。\n"
            "- 故障问题：我会按缺失情况追问环境、操作路径、模块分支和日志。\n"
            "- 需要查代码时只索取「模块 + 分支」，例如 `yicall-common release/3.1`，"
            "不会要求选择项目或客户现场。\n"
            "- 追问阶段回复「直接分析」可跳过补充；回复「取消」结束本次分析；"
            "回复「重置会话」清空上下文。"
        )

    def _pending_menu_text(self, question, menu) -> str:
        """在两级菜单后附上待回答的问题，提示选完项目会自动开始分析。"""
        preview = question if len(question) <= 120 else question[:120] + "…"
        return (
            f"{menu}\n\n"
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
        interaction = getattr(self._settings, "interaction", None)
        if getattr(interaction, "fault_dialog_enabled", False):
            lines.extend(
                [
                    "",
                    "现在可以直接 @我 做代码问答。",
                    "如果遇到报错、超时、空白或崩溃，直接描述现象；我会先确认环境、操作路径、版本和日志，再进入故障分析。",
                    "故障追问阶段回复「直接分析」可跳过补充。",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "现在可以直接 @我 做代码问答。",
                    "如果遇到报错、超时、空白或崩溃，直接描述现象，我会进入故障分析。",
                ]
            )
        lines.append("发送「@我 切换项目」可重新选择。")
        return "\n".join(lines)

    async def _route_question(self, project, question, body, req_id):
        """按故障、知识库、代码三种模式路由；未启用新能力时保持旧链路。"""
        fault_enabled = bool(getattr(self._settings.fault_analysis, "enabled", True))
        interaction = getattr(self._settings, "interaction", None)
        dialog_enabled = bool(getattr(interaction, "fault_dialog_enabled", False))
        knowledge_first = bool(getattr(interaction, "knowledge_first", False))

        if fault_enabled and self._is_fault_request(question):
            if dialog_enabled:
                await self._begin_fault_dialog(project, question, body, req_id)
                return
            await self._start_answer(project, question, body, req_id, mode="fault")
            return

        knowledge_ready = bool(
            getattr(self._settings.knowledge, "enabled", False)
            and getattr(self._settings.confluence, "configured", False)
        )
        if (
            knowledge_first
            and knowledge_ready
            and (
                (
                    fault_analysis.is_document_question(question)
                    and not fault_analysis.has_fault_evidence(question)
                )
                or not fault_analysis.is_code_question(question)
            )
        ):
            await self._start_answer(project, question, body, req_id, mode="knowledge")
            return
        await self._start_answer(project, question, body, req_id, mode="code")

    async def _begin_fault_dialog(
        self, project, question, body, req_id, mode="fault"
    ):
        """创建追问上下文；项目只作为可选的代码分支快捷映射。"""
        chatid = str(body.get("chatid") or "")
        userid = str((body.get("from") or {}).get("userid") or "")
        mode = "code" if str(mode or "").lower() == "code" else "fault"
        dialog = fault_dialog.new_dialog(
            question,
            project.key if project is not None else "",
            project.name if project is not None else "",
            mode=mode,
        )
        product = fault_analysis.match_product(self._settings, question)
        if product is None and project is not None:
            product = self._settings.product_by_key(project.product_key)
        if product is not None:
            dialog["product_key"] = product.key
        prompt = fault_dialog.next_prompt(
            dialog, self._fault_dialog_max_turns()
        )
        if prompt:
            self._save_fault_dialog(project, dialog, chatid, userid)
            if project is not None:
                lead = f"已识别项目：**{project.name}**。\n\n"
            elif product is not None:
                lead = f"已识别产品：**{product.name}**。\n\n"
            elif mode == "code":
                lead = "收到代码问题。\n\n"
            else:
                lead = "收到故障描述。\n\n"
            lead += "代码分析只按「模块 + 分支」定位，不需要你选择项目或客户现场。\n\n"
            await self._reply(
                req_id,
                lead
                + "为避免仅凭现象误判，我还需要确认一项信息：\n\n"
                f"{prompt}",
            )
            return

        gap = self._module_version_gap(dialog, project)
        if gap:
            await self._ask_code_version(project, dialog, gap, chatid, userid, req_id)
            return
        await self._start_answer(
            project,
            question,
            body,
            req_id,
            mode=mode,
            fault_context=dialog,
            code_targets=self._resolve_code_targets(dialog, project),
        )

    async def _continue_fault_dialog(self, project, dialog, answer, body, req_id):
        """推进一轮追问；信息齐全后按模块分支检出代码并分析。"""
        chatid = str(body.get("chatid") or "")
        userid = str((body.get("from") or {}).get("userid") or "")
        mode = str(dialog.get("mode") or "fault")
        max_turns = self._fault_dialog_max_turns()

        if fault_dialog.is_cancel_command(answer):
            self._session.clear_fault_dialog(chatid, userid)
            await self._reply(req_id, "已取消本次分析。需要时重新描述问题即可。")
            return

        skip = fault_dialog.is_skip_command(answer)
        if not skip:
            dialog = fault_dialog.record_answer(dialog, answer)
            project = project or self._resolve_dialog_project(dialog)
            if project is not None:
                dialog = fault_dialog.set_project(
                    dialog, project.key, project.name
                )
            prompt = fault_dialog.next_prompt(dialog, max_turns)
            if prompt:
                self._save_fault_dialog(project, dialog, chatid, userid)
                await self._reply(
                    req_id,
                    "已收到，继续确认下一项：\n\n"
                    f"{prompt}",
                )
                return
        else:
            project = project or self._resolve_dialog_project(dialog)
            if project is not None:
                dialog = fault_dialog.set_project(
                    dialog, project.key, project.name
                )

        gap = self._module_version_gap(dialog, project)
        if gap:
            if not skip and int(dialog.get("turns") or 0) < max_turns:
                await self._ask_code_version(
                    project, dialog, gap, chatid, userid, req_id
                )
                return
            await self._fallback_to_knowledge(dialog, body, req_id, chatid, userid)
            return

        targets = self._resolve_code_targets(dialog, project)
        if not targets:
            await self._fallback_to_knowledge(dialog, body, req_id, chatid, userid)
            return
        sent = await self._start_answer(
            project,
            dialog.get("original_question") or "",
            body,
            req_id,
            mode=mode,
            fault_context=dialog,
            code_targets=targets,
        )
        if sent:
            self._session.clear_fault_dialog(chatid, userid)

    def _module_version_gap(self, dialog, project):
        """检查模块/分支是否足以检出代码，返回缺失项类型。"""
        slots = dialog.get("slots") or {}
        raw = str(slots.get("module_version") or "").strip()
        parsed = []
        if raw and raw != "用户未提供":
            parsed = fault_analysis.parse_code_targets(self._settings, raw)
        if not parsed:
            parsed = fault_analysis.parse_code_targets(
                self._settings, fault_dialog.analysis_query(dialog)
            )
        if not parsed:
            # 已识别项目时可以直接沿用项目配置的分支；没有项目才需要用户给模块。
            if project is not None and project.module_items():
                return ""
            return "module"
        branches = dict(project.module_items()) if project is not None else {}
        for target in parsed:
            if not target.get("branch") and not branches.get(target["module"]):
                return "branch"
        return ""

    def _resolve_code_targets(self, dialog, project):
        """把追问得到的模块/分支转成代码检出目标；项目只用于补全分支。"""
        slots = dialog.get("slots") or {}
        raw = str(slots.get("module_version") or "").strip()
        parsed = []
        if raw and raw != "用户未提供":
            parsed = fault_analysis.parse_code_targets(self._settings, raw)
        if not parsed:
            parsed = fault_analysis.parse_code_targets(
                self._settings, fault_dialog.analysis_query(dialog)
            )

        branches = dict(project.module_items()) if project is not None else {}
        if parsed:
            targets = []
            for target in parsed:
                module = str(target.get("module") or "").strip()
                branch = str(target.get("branch") or branches.get(module) or "").strip()
                if module and branch:
                    targets.append({"module": module, "branch": branch})
            return self._dedupe_targets(targets)

        if project is None:
            return []
        if str(dialog.get("mode") or "fault") == "fault":
            modules = fault_analysis.select_modules(
                project,
                fault_dialog.analysis_query(dialog),
                self._settings.fault_analysis.max_modules,
            )
        else:
            modules = project.module_items()
        return self._dedupe_targets(
            [{"module": module, "branch": branch} for module, branch in modules]
        )

    def _dedupe_targets(self, targets):
        """按模块+分支去重，避免重复检出同一个工作树。"""
        result = []
        seen = set()
        for target in targets or []:
            key = (target.get("module"), target.get("branch"))
            if key in seen:
                continue
            seen.add(key)
            result.append(target)
        return result

    async def _ask_code_version(
        self, project, dialog, gap, chatid, userid, req_id
    ):
        """只追问代码分支缺口，不再询问项目或客户现场。"""
        dialog = fault_dialog.request_slot(dialog, "module_version")
        self._save_fault_dialog(project, dialog, chatid, userid)
        slots = dialog.get("slots") or {}
        raw = str(slots.get("module_version") or "").strip()
        parsed = fault_analysis.parse_code_targets(self._settings, raw)
        if not parsed:
            parsed = fault_analysis.parse_code_targets(
                self._settings, fault_dialog.analysis_query(dialog)
            )
        modules = "、".join(
            f"`{item['module']}`" for item in parsed if item.get("module")
        )
        if gap == "branch":
            hint = (
                f"已识别模块：{modules}，但还缺代码分支/版本。\n\n"
                "请直接回复分支，例如 `release/3.1`；也可以回复完整的 "
                "`模块名 release/3.1`。"
            )
        else:
            hint = (
                "还缺出问题的模块名。请直接回复模块名和分支，例如 "
                "`yicall-common release/3.1`；只给模块名也可以，我会再确认分支。"
            )
        await self._reply(
            req_id,
            hint
            + "\n\n如果暂时拿不到分支，回复「直接分析」，我会先按 Confluence "
            "已同步资料给出排查建议。",
        )

    async def _fallback_to_knowledge(self, dialog, body, req_id, chatid, userid):
        """没有可用代码分支时退回知识库，避免把用户卡在追问里。"""
        knowledge_ready = bool(
            getattr(self._settings.knowledge, "enabled", False)
            and getattr(self._settings.confluence, "configured", False)
        )
        if not knowledge_ready:
            self._session.clear_fault_dialog(chatid, userid)
            await self._reply(
                req_id,
                "当前无法定位代码分支。请补充模块名和分支/版本后重新提问，"
                "例如 `yicall-common release/3.1`。",
            )
            return
        await self._reply(
            req_id,
            "没有拿到可用的代码分支，我先按 Confluence 已同步资料给出排查建议。",
        )
        sent = await self._start_answer(
            None,
            fault_dialog.format_context(dialog),
            body,
            req_id,
            mode="knowledge",
        )
        if sent:
            self._session.clear_fault_dialog(chatid, userid)

    def _fault_dialog_max_turns(self):
        """读取追问轮次上限，旧配置缺失时使用 4 轮。"""
        interaction = getattr(self._settings, "interaction", None)
        return max(1, int(getattr(interaction, "fault_dialog_max_turns", 4) or 4))

    async def _start_answer(
        self,
        project,
        question,
        body,
        req_id,
        mode="code",
        fault_context=None,
        code_targets=None,
    ):
        """投递一次问答任务，并先推一条「正在分析」的流式占位消息。"""
        chatid = str(body.get("chatid") or "")
        chattype = str(body.get("chattype") or "")
        userid = str((body.get("from") or {}).get("userid") or "")
        task_id = f"cb-{uuid.uuid4().hex}"
        stream_id = protocol.new_stream_id()
        started_at = time.time()
        if mode == "knowledge":
            label = f"（{project.name}）" if project is not None else ""
            placeholder = (
                f"正在查询 **Confluence 知识库**{label}…\n\n"
                "只使用已同步资料作答，并给出可核对的页面来源，请稍候。"
            )
            project_key = project.key if project is not None else ""
            project_name = project.name if project is not None else "Confluence 知识库"
        elif code_targets:
            modules = "、".join(
                f"`{item.get('module')} {item.get('branch')}`"
                for item in code_targets[:4]
            )
            label = f"**{project.name}** 的" if project is not None else ""
            placeholder = (
                f"正在分析 {label}{len(code_targets)} 个指定模块代码…\n\n"
                f"{modules}\n\n首次分析需要拉取仓库，通常 1~3 分钟，请稍候。"
            )
            project_key = project.key if project is not None else ""
            project_name = project.name if project is not None else "指定模块"
        elif project is None:
            placeholder = (
                "正在查询 **Confluence 知识库**…\n\n"
                "我会从已同步资料中给出一个结论，请稍候。"
            )
            project_key = ""
            project_name = "Confluence 知识库"
        elif mode == "fault":
            placeholder = (
                f"正在对 **{project.name}** 做故障分析…\n\n"
                "我会先定位相关模块，再结合代码和知识库资料给出原因与验证步骤。"
            )
            project_key = project.key
            project_name = project.name
        else:
            modules = project.module_items()
            placeholder = (
                f"正在分析 **{project.name}** 的 {len(modules)} 个模块代码…\n\n"
                "首次分析需要拉取仓库，通常 1~3 分钟，请稍候。"
            )
            project_key = project.key
            project_name = project.name
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
            "project_key": project_key,
            "project_name": project_name,
            "question": question,
            "answer_mode": str(mode or "code"),
            "started_at": started_at,
        }
        if fault_context:
            context["fault_context"] = fault_context
        if code_targets:
            context["code_targets"] = code_targets
        if str(mode or "") == "knowledge":
            # 普通问题走知识库时强制要求有效引用；旧的知识库任务调用不传该字段。
            context["strict_citation"] = True
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
            return False
        return True

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

