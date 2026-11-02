"""机器人网关测试：覆盖配置解析、协议辅助、会话存储与消息路由。"""
import asyncio
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.chatbot import repos  # noqa: E402
from app.chatbot.wecom import protocol  # noqa: E402
from app.chatbot.session import ChatSessionStore  # noqa: E402
from app.chatbot.settings import ChatbotConfigError, load_settings  # noqa: E402
from app.chatbot.tasks import _truncate_utf8  # noqa: E402
from app.chatbot.wecom import client as client_module  # noqa: E402
from app.chatbot.wecom.client import WeComConnectionError, WeComLongConnection  # noqa: E402
from app.chatbot.wecom.gateway import ChatbotGateway  # noqa: E402


# 测试只用环境变量提供机器人凭证；项目映射来自仓库内的 config/chatbot.json。
CHATBOT_ENV = {
    "CHATBOT_BOT_ID": "",
    "CHATBOT_BOT_SECRET": "",
}
os.environ.update(CHATBOT_ENV)


class FakeRedis:
    """最小可用的 Redis 替身，只实现会话模块用到的命令。"""

    def __init__(self):
        """初始化内存存储。"""
        self.data = {}
        self.published = []

    def get(self, key):
        """读取字符串值。"""
        return self.data.get(key)

    def set(self, key, value, nx=False, ex=None):
        """写入字符串值，支持 NX 语义。"""
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    def delete(self, key):
        """删除键。"""
        self.data.pop(key, None)
        return 1

    def publish(self, channel, message):
        """记录发布内容，便于断言。"""
        self.published.append((channel, message))
        return 1


class FakeConnection:
    """记录网关发出的各类消息，避免真的连企微。"""

    def __init__(self):
        """初始化各消息缓冲。"""
        self.streams = []
        self.markdowns = []
        self.welcomes = []
        self.pushed = []

    async def send_stream(self, req_id, stream_id, content, finish, feedback_id=None):
        """记录流式消息。"""
        self.streams.append({"content": content, "finish": finish, "stream_id": stream_id})

    async def send_markdown(self, req_id, content, feedback_id=None):
        """记录 markdown 回复。"""
        self.markdowns.append(content)

    async def send_welcome(self, req_id, content):
        """记录欢迎语。"""
        self.welcomes.append(content)

    async def push_markdown(self, chatid, content, chat_type=0):
        """记录主动推送。"""
        self.pushed.append({"chatid": chatid, "content": content, "chat_type": chat_type})


class BrokenStreamConnection(FakeConnection):
    """模拟流式窗口已失效：所有流式刷新都失败，用于验证主动推送降级。"""

    async def send_stream(self, req_id, stream_id, content, finish, feedback_id=None):
        """始终抛出异常，触发网关的兜底逻辑。"""
        raise RuntimeError("stream expired")

def build_settings(**gateway_overrides):
    """加载环境变量配置，并按需覆盖网关参数（用于缩短打字机间隔）。"""
    settings = load_settings()
    if gateway_overrides:
        settings = dataclasses.replace(
            settings, gateway=dataclasses.replace(settings.gateway, **gateway_overrides)
        )
    return settings


def group_message(content, msgid="m-1", userid="user-1", chatid="chat-1"):
    """构造一条群聊文本回调体。"""
    return {
        "msgid": msgid,
        "chattype": "group",
        "chatid": chatid,
        "from": {"userid": userid},
        "msgtype": "text",
        "text": {"content": content},
    }


class SettingsTest(unittest.TestCase):
    """校验项目映射与序号选择。"""

    def test_repository_config_matches_excel_mapping(self):
        """仓库内配置应与 Excel 的 5 个项目、11 个模块口径一致。"""
        settings = load_settings()
        names = [item.name for item in settings.projects]
        self.assertEqual(
            names, ["call3.1主线", "call3.1招商证券", "call3.0兴业银行", "call3.0天宏基金", "call3.0富国基金"]
        )
        # 主线 10 个模块、招商 9 个、其余各 5 个（空单元格表示不使用该模块）。
        self.assertEqual([len(p.module_items()) for p in settings.projects], [10, 9, 5, 5, 5])
        main = settings.project_by_key("yicall-3.1-main")
        self.assertEqual(main.modules["llm-management"], "release/3.2")
        self.assertNotIn("yicall-management-system", main.modules)
        xyyh = settings.project_by_key("yicall-3.0-xyyh")
        self.assertEqual(xyyh.modules["fe-yicall-web"], "release/3.1-xyyh")

    def test_project_by_index_and_out_of_range(self):
        """序号按 1 起始解析，越界返回 None。"""
        settings = load_settings()
        self.assertEqual(settings.project_by_index("2").key, "yicall-3.1-zszq")
        self.assertIsNone(settings.project_by_index("9"))
        self.assertIsNone(settings.project_by_index("abc"))

    def test_every_configured_module_has_an_explicit_repo_url(self):
        """所有用到的模块都必须配置显式仓库地址，避免同名搜索串仓或漏配。"""
        settings = load_settings()
        used = {module for project in settings.projects for module, _branch in project.module_items()}
        mapped = set(settings.repos.module_repo_urls)

        self.assertEqual(used - mapped, set())
        self.assertEqual(
            settings.repos.module_repo_urls["aiforce-web"],
            "https://code.in.wezhuiyi.com/aiforce/aiforce-web.git",
        )
        self.assertEqual(
            settings.repos.module_repo_urls["fe-yicall-web"],
            "https://code.in.wezhuiyi.com/yicall/fe-yicall-web.git",
        )
        self.assertEqual(
            settings.repos.module_repo_urls["llm-management"],
            "https://code.in.wezhuiyi.com/yicall/llm-management.git",
        )
        self.assertEqual(
            settings.repos.module_repo_urls["asr-tts-adaptor"],
            "https://code.in.wezhuiyi.com/asr-tts-adaptor/asr-tts-adaptor.git",
        )

    def test_menu_lists_every_project(self):
        """菜单必须包含全部项目与切换提示。"""
        menu = load_settings().menu_text()
        for name in ["call3.1主线", "call3.1招商证券", "call3.0兴业银行"]:
            self.assertIn(name, menu)
        self.assertIn("切换项目", menu)
        # 群聊只有 @机器人 才会收到回调，菜单必须明确要求重新 @。
        self.assertIn("@我", menu)

    def test_invalid_config_raises(self):
        """项目缺少 name 时要抛出可读错误，并指出配置文件。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "chatbot.json"
            path.write_text(
                json.dumps({"projects": [{"key": "only-key", "modules": {}}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaises(ChatbotConfigError):
                load_settings(path)

    def test_answer_uses_container_isolated_sandbox(self):
        """容器已提供隔离，问答不能使用依赖 bwrap 的只读沙箱。"""
        self.assertEqual(load_settings().answer.sandbox, "danger-full-access")

    def test_bot_credentials_come_from_env_and_secret_is_masked(self):
        """机器人凭证只读环境变量，Secret 不能出现在 repr 中。"""
        env = {"CHATBOT_BOT_ID": "bot-id", "CHATBOT_BOT_SECRET": "top-secret"}
        with patch.dict(os.environ, env, clear=False):
            settings = load_settings()
        self.assertTrue(settings.bot.configured)
        self.assertNotIn("top-secret", repr(settings))


class ProtocolTest(unittest.TestCase):
    """校验报文解析与文本切分。"""

    def test_strip_mentions_handles_multiple_and_special_space(self):
        """多个 @ 前缀与四分之一空格都要被剥掉。"""
        self.assertEqual(protocol.strip_mentions("@RobotA hello robot"), "hello robot")
        self.assertEqual(protocol.strip_mentions("@A\u2005@B 回调入口在哪"), "回调入口在哪")
        self.assertEqual(protocol.strip_mentions("@A"), "")

    def test_extract_text_ignores_non_text(self):
        """非文本消息返回空串。"""
        self.assertEqual(protocol.extract_text({"msgtype": "image"}), "")
        self.assertEqual(
            protocol.extract_text({"msgtype": "text", "text": {"content": "@B 问题"}}), "问题"
        )

    def test_split_for_typewriter_respects_limits(self):
        """分段数量不超过上限，且拼接后内容不丢。"""
        content = "\n".join(f"第{i}行内容" for i in range(200))
        parts = protocol.split_for_typewriter(content, 80, 5)
        self.assertLessEqual(len(parts), 5)
        self.assertEqual("".join(parts), content)

    def test_split_utf8_never_breaks_multibyte(self):
        """按字节切分后每段都不超过上限，且能还原原文。"""
        text = "中文内容" * 1000
        chunks = protocol.split_utf8(text, 1000)
        self.assertTrue(all(len(c.encode("utf-8")) <= 1000 for c in chunks))
        self.assertEqual("".join(chunks), text)

    def test_server_errcode_is_treated_as_failure(self):
        """服务端返回非 0 errcode 时必须抛异常，让网关走降级推送。"""
        self.assertEqual(WeComLongConnection._ensure_success({"errcode": 0}, "发送")["errcode"], 0)
        with self.assertRaises(WeComConnectionError):
            WeComLongConnection._ensure_success({"errcode": 45009, "errmsg": "rate limit"}, "发送")


class FakeWebSocket:
    """最小 WebSocket 替身：收到订阅帧后把响应放回接收队列。"""

    def __init__(self):
        """初始化发送记录与接收队列。"""
        self.sent = []
        self.incoming = asyncio.Queue()

    async def send(self, raw):
        """记录上行帧，并对订阅请求返回成功响应。"""
        frame = json.loads(raw)
        self.sent.append(frame)
        if frame.get("cmd") == protocol.CMD_SUBSCRIBE:
            await self.incoming.put(
                {
                    "headers": {"req_id": (frame.get("headers") or {}).get("req_id")},
                    "errcode": 0,
                    "errmsg": "ok",
                }
            )
            await self.incoming.put(None)

    def __aiter__(self):
        """支持 async for 读取下行帧。"""
        return self

    async def __anext__(self):
        """队列里的 None 表示服务端关闭连接。"""
        raw = await self.incoming.get()
        if raw is None:
            raise StopAsyncIteration
        return json.dumps(raw, ensure_ascii=False)

    async def close(self):
        """关闭连接。"""
        return None


class FakeConnect:
    """模拟 websockets.connect() 的异步上下文管理器。"""

    def __init__(self, websocket):
        """保存待返回的连接对象。"""
        self.websocket = websocket

    async def __aenter__(self):
        """进入上下文时返回 WebSocket。"""
        return self.websocket

    async def __aexit__(self, exc_type, exc, tb):
        """退出上下文时不吞异常。"""
        return False


class ClientReceiveLoopTest(unittest.IsolatedAsyncioTestCase):
    """验证订阅响应由收包循环读取，不会被 request() 的自锁卡住。"""

    async def test_subscribe_response_is_read_while_request_waits(self):
        """订阅请求发出后应立即读到响应，而不是等到超时。"""
        websocket = FakeWebSocket()
        fake_module = SimpleNamespace(connect=lambda *args, **kwargs: FakeConnect(websocket))
        callbacks = []

        async def on_callback(payload):
            callbacks.append(payload)

        connection = WeComLongConnection(
            "wss://example.invalid",
            "bot-id",
            "secret",
            on_callback,
            heartbeat_seconds=300,
        )
        with patch.object(client_module, "websockets", fake_module):
            await asyncio.wait_for(connection._serve_once(), timeout=2)

        self.assertEqual(websocket.sent[0]["cmd"], protocol.CMD_SUBSCRIBE)
        self.assertEqual(websocket.sent[0]["body"]["bot_id"], "bot-id")


class TaskHelpersTest(unittest.TestCase):
    """校验审计表写入前的文本保护。"""

    def test_truncate_utf8_keeps_multibyte_and_marks_cut(self):
        """超长中文按字节截断后仍是合法 UTF-8，并带明确提示。"""
        text = "中" * 30000
        result = _truncate_utf8(text, limit=1000)
        self.assertLessEqual(len(result.encode("utf-8")), 1100)
        self.assertIn("已截断存储", result)
        result.encode("utf-8")


class ReposTest(unittest.TestCase):
    """校验仓库地址与路径安全处理。"""

    def test_safe_segment_blocks_path_traversal(self):
        """分支名中的斜杠与 .. 必须被替换掉。"""
        self.assertEqual(repos.safe_segment("release/3.1"), "release-3.1")
        self.assertNotIn("..", repos.safe_segment("../../etc/passwd"))

    def test_auth_url_injects_credentials_only_for_http(self):
        """http(s) 地址注入凭据，ssh 地址保持原样。"""
        url = repos.build_authenticated_url("https://git.example.com/g/a.git", "u", "p")
        self.assertEqual(url, "https://u:p@git.example.com/g/a.git")
        self.assertEqual(
            repos.build_authenticated_url("git@git.example.com:g/a.git", "u", "p"),
            "git@git.example.com:g/a.git",
        )

    def test_fetch_url_skips_username_without_password(self):
        """只有用户名没有密码时不能拼出会触发交互输入的地址。"""
        settings = build_settings()
        settings = dataclasses.replace(
            settings,
            repos=dataclasses.replace(
                settings.repos, username="u", password="", private_token=""
            ),
        )
        self.assertEqual(
            repos.resolve_fetch_url(settings, "https://git.example.com/g/a.git"),
            "https://git.example.com/g/a.git",
        )

    def test_resolve_repo_url_prefers_explicit_mapping(self):
        """显式映射优先于 GitLab 搜索。"""
        settings = build_settings()
        settings = dataclasses.replace(
            settings,
            repos=dataclasses.replace(
                settings.repos, module_repo_urls={"yicall-common": "https://g/x/yicall-common.git"}
            ),
        )
        self.assertEqual(repos.resolve_repo_url("yicall-common", settings), "https://g/x/yicall-common.git")

    def test_redact_credentials_removes_userinfo(self):
        """错误信息中的账号密码或令牌必须被隐藏。"""
        raw = "fatal: https://oauth2:secret-token@git.example.com/g/a.git not found"
        self.assertEqual(
            repos.redact_credentials(raw),
            "fatal: https://***@git.example.com/g/a.git not found",
        )

    def test_prepare_cache_repo_and_worktree_roundtrip(self):
        """用真实 git 仓库验证缓存仓库、浅拉取、工作树与清理流程。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()

            def run_git(*args):
                """在临时源仓库执行一条 git 命令。"""
                completed = subprocess.run(
                    ["git", *args],
                    cwd=str(source),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return completed.stdout

            run_git("init", "--quiet", "-b", "main")
            (source / "demo.txt").write_text("hello", encoding="utf-8")
            run_git("add", ".")
            run_git(
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "init",
            )
            run_git("branch", "release/test")

            settings = build_settings()
            settings = dataclasses.replace(
                settings,
                repos=dataclasses.replace(
                    settings.repos,
                    cache_dir=str(root / "cache"),
                    module_repo_urls={"demo": source.as_uri()},
                    clone_timeout_seconds=60,
                ),
            )
            repo_dir, cache_ref = repos.prepare_cache_repo("demo", "release/test", settings)
            worktree = repos.create_worktree(repo_dir, cache_ref, root / "worktree", settings)
            self.assertEqual((worktree / "demo.txt").read_text(encoding="utf-8"), "hello")

            # 第二次拉取必须把缓存 ref 更新到最新提交，供下一次问答使用。
            run_git("checkout", "release/test")
            (source / "v2.txt").write_text("second", encoding="utf-8")
            run_git("add", ".")
            run_git(
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "second",
            )
            repo_dir, cache_ref = repos.prepare_cache_repo("demo", "release/test", settings)
            worktree_v2 = repos.create_worktree(repo_dir, cache_ref, root / "worktree-v2", settings)
            self.assertEqual((worktree_v2 / "v2.txt").read_text(encoding="utf-8"), "second")

            repos.remove_worktree(repo_dir, worktree, settings)
            repos.remove_worktree(repo_dir, worktree_v2, settings)
            self.assertFalse(worktree.exists())
            self.assertFalse(worktree_v2.exists())

class SessionTest(unittest.TestCase):
    """校验 Redis 会话行为。"""

    def setUp(self):
        """每个用例使用独立的假 Redis。"""
        self.redis = FakeRedis()
        self.store = ChatSessionStore(self.redis, ttl_seconds=600)

    def test_project_roundtrip_and_clear(self):
        """选中项目后可读回，清除后为空。"""
        self.store.set_project("chat-1", "user-1", "yicall-3.1-main")
        self.assertEqual(self.store.get_project_key("chat-1", "user-1"), "yicall-3.1-main")
        self.store.clear_project("chat-1", "user-1")
        self.assertEqual(self.store.get_project_key("chat-1", "user-1"), "")

    def test_pending_question_survives_project_selection(self):
        """待回答问题在选中项目后仍可取出，且只消费一次。"""
        self.store.set_pending_question("chat-1", "user-1", "回调入口在哪")
        self.store.set_project("chat-1", "user-1", "yicall-3.1-main")
        self.assertEqual(self.store.pop_pending_question("chat-1", "user-1"), "回调入口在哪")
        self.assertEqual(self.store.pop_pending_question("chat-1", "user-1"), "")
        self.assertEqual(self.store.get_project_key("chat-1", "user-1"), "yicall-3.1-main")

    def test_clear_project_keeps_pending_question(self):
        """切换项目只清除选择，挂起的问题要保留到重新选完项目。"""
        self.store.set_pending_question("chat-1", "user-1", "回调入口在哪")
        self.assertEqual(self.store.clear_project("chat-1", "user-1"), "回调入口在哪")
        self.assertEqual(self.store.pop_pending_question("chat-1", "user-1"), "回调入口在哪")

    def test_msgid_is_deduplicated(self):
        """同一 msgid 只有第一次抢占成功。"""
        self.assertTrue(self.store.claim_msgid("m-1"))
        self.assertFalse(self.store.claim_msgid("m-1"))
        self.assertTrue(self.store.claim_msgid("m-2"))

    def test_stream_context_roundtrip(self):
        """流式上下文可存取与删除。"""
        self.store.save_stream("t-1", {"req_id": "r", "stream_id": "s"})
        self.assertEqual(self.store.load_stream("t-1")["stream_id"], "s")
        self.store.drop_stream("t-1")
        self.assertEqual(self.store.load_stream("t-1"), {})


class GatewayTest(unittest.IsolatedAsyncioTestCase):
    """校验群消息路由与结果推送。"""

    def build(self, **gateway_overrides):
        """构造网关并替换为假连接与假任务队列。"""
        settings = build_settings(**gateway_overrides)
        redis = FakeRedis()
        self.store = ChatSessionStore(redis, ttl_seconds=600)
        self.enqueued = []
        gateway = ChatbotGateway(settings, self.store, self.enqueued.append)
        self.conn = FakeConnection()
        gateway._connection = self.conn
        return gateway

    async def test_single_chat_gets_notice(self):
        """单聊直接提示去群里使用。"""
        gateway = self.build()
        body = group_message("@Bot hi")
        body["chattype"] = "single"
        await gateway._handle_message(body, "req-1")
        self.assertEqual(len(self.conn.markdowns), 1)
        self.assertIn("群里", self.conn.markdowns[0])

    async def test_question_without_project_shows_menu(self):
        """未选项目时先返回菜单并暂存问题，不投递任务。"""
        gateway = self.build()
        await gateway._handle_message(group_message("@Bot 回调入口在哪"), "req-1")
        self.assertEqual(self.enqueued, [])
        self.assertIn("请先选择", self.conn.markdowns[0])
        self.assertIn("回调入口在哪", self.conn.markdowns[0])
        self.assertEqual(self.store.pop_pending_question("chat-1", "user-1"), "回调入口在哪")

    async def test_select_project_after_question_answers_pending_question(self):
        """先提问再选项目时，直接沿用原问题发起分析，不再要求重复提问。"""
        gateway = self.build()
        await gateway._handle_message(group_message("@Bot 结果回调入口在哪", msgid="m-1"), "req-1")
        await gateway._handle_message(group_message("@Bot 1", msgid="m-2"), "req-2")

        self.assertEqual(len(self.enqueued), 1)
        self.assertEqual(self.enqueued[0]["project_key"], "yicall-3.1-main")
        self.assertEqual(self.enqueued[0]["question"], "结果回调入口在哪")
        self.assertEqual(self.store.get_project_key("chat-1", "user-1"), "yicall-3.1-main")
        self.assertEqual(self.store.pop_pending_question("chat-1", "user-1"), "")

    async def test_switch_project_keeps_pending_question(self):
        """先提问再切换项目时，重新选完项目仍直接回答原问题。"""
        gateway = self.build()
        await gateway._handle_message(group_message("@Bot 结果回调入口在哪", msgid="m-1"), "req-1")
        await gateway._handle_message(group_message("@Bot 切换项目", msgid="m-2"), "req-2")
        self.assertIn("结果回调入口在哪", self.conn.markdowns[-1])

        await gateway._handle_message(group_message("@Bot 2", msgid="m-3"), "req-3")
        self.assertEqual(len(self.enqueued), 1)
        self.assertEqual(self.enqueued[0]["project_key"], "yicall-3.1-zszq")
        self.assertEqual(self.enqueued[0]["question"], "结果回调入口在哪")

    async def test_enqueue_failure_ends_stream_and_cleans_context(self):
        """任务投递失败时要结束流式消息，并清掉不再有用的上下文。"""
        settings = build_settings()
        redis = FakeRedis()
        store = ChatSessionStore(redis, ttl_seconds=600)
        store.set_project("chat-1", "user-1", "yicall-3.1-main")

        def fail(_payload):
            """模拟 Celery Broker 不可用。"""
            raise RuntimeError("broker down")

        gateway = ChatbotGateway(settings, store, fail)
        self.conn = FakeConnection()
        gateway._connection = self.conn
        await gateway._handle_message(group_message("@Bot 回调入口在哪"), "req-1")

        self.assertTrue(self.conn.streams[-1]["finish"])
        self.assertIn("投递失败", self.conn.streams[-1]["content"])
        self.assertFalse(any(key.startswith("chatbot:stream:") for key in redis.data))

    async def test_select_project_by_index(self):
        """回复序号即可选中项目，并回显模块分支。"""
        gateway = self.build()
        await gateway._handle_message(group_message("@Bot 1"), "req-1")
        self.assertEqual(self.store.get_project_key("chat-1", "user-1"), "yicall-3.1-main")
        self.assertIn("call3.1主线", self.conn.markdowns[0])
        self.assertIn("release/3.1", self.conn.markdowns[0])

    async def test_duplicate_msgid_is_ignored(self):
        """重复推送的 msgid 不再触发任何回复。"""
        gateway = self.build()
        body = group_message("@Bot 1", msgid="dup-1")
        await gateway._handle_message(body, "req-1")
        await gateway._handle_message(body, "req-2")
        self.assertEqual(len(self.conn.markdowns), 1)

    async def test_question_enqueues_task_and_starts_stream(self):
        """选中项目后提问会先推流式占位，再投递 Celery。"""
        gateway = self.build()
        self.store.set_project("chat-1", "user-1", "yicall-3.0-xyyh")
        await gateway._handle_message(group_message("@Bot 结果回调在哪"), "req-1")

        self.assertEqual(len(self.enqueued), 1)
        payload = self.enqueued[0]
        self.assertEqual(payload["project_key"], "yicall-3.0-xyyh")
        self.assertEqual(payload["question"], "结果回调在哪")
        self.assertTrue(payload["task_id"].startswith("cb-"))
        self.assertEqual(self.conn.streams[0]["finish"], False)
        self.assertIn("call3.0兴业银行", self.conn.streams[0]["content"])

    async def test_switch_project_clears_selection(self):
        """「切换项目」清空选择并重新给菜单。"""
        gateway = self.build()
        self.store.set_project("chat-1", "user-1", "yicall-3.1-main")
        await gateway._handle_message(group_message("@Bot 切换项目"), "req-1")
        self.assertEqual(self.store.get_project_key("chat-1", "user-1"), "")
        self.assertIn("请先选择", self.conn.markdowns[0])

    async def test_enter_chat_sends_welcome(self):
        """进入单聊事件要提示用户去群里使用，不能展示项目菜单。"""
        gateway = self.build()
        await gateway._handle_event({"event": {"eventtype": "enter_chat"}}, "req-1")
        self.assertEqual(len(self.conn.welcomes), 1)
        self.assertIn("群里", self.conn.welcomes[0])
        self.assertNotIn("call3.1主线", self.conn.welcomes[0])
    async def test_deliver_typewriter_finishes_stream(self):
        """正常结果按分段刷新，最后一条 finish=True 且是完整内容。"""
        gateway = self.build(typewriter_chunk_chars=10, typewriter_interval_ms=500, typewriter_max_chunks=5)
        self.store.save_stream(
            "t-1",
            {"req_id": "req-1", "stream_id": "s-1", "chatid": "chat-1", "chattype": "group"},
        )
        reply = {
            "task_id": "t-1",
            "req_id": "req-1",
            "stream_id": "s-1",
            "chatid": "chat-1",
            "chattype": "group",
            "ok": True,
            "answer": "第一段\n第二段\n第三段",
            "started_at": 0,
        }
        await gateway._deliver(reply)

        self.assertTrue(self.conn.streams)
        self.assertTrue(self.conn.streams[-1]["finish"])
        self.assertEqual(self.conn.streams[-1]["content"], "第一段\n第二段\n第三段")
        self.assertEqual(self.conn.pushed, [])
        # 交付后上下文应被清掉，避免重复推送。
        self.assertEqual(self.store.load_stream("t-1"), {})

    async def test_deliver_after_deadline_pushes_markdown(self):
        """超过软截止时间时结束流式消息并改用主动推送。"""
        gateway = self.build(stream_soft_deadline_seconds=1)
        self.store.save_stream(
            "t-2",
            {"req_id": "req-2", "stream_id": "s-2", "chatid": "chat-9", "chattype": "group"},
        )
        reply = {
            "task_id": "t-2",
            "req_id": "req-2",
            "stream_id": "s-2",
            "chatid": "chat-9",
            "chattype": "group",
            "ok": True,
            "answer": "分析结果",
            "started_at": 1,
        }
        await gateway._deliver(reply)

        self.assertTrue(self.conn.streams[-1]["finish"])
        self.assertEqual(len(self.conn.pushed), 1)
        self.assertEqual(self.conn.pushed[0]["chatid"], "chat-9")
        self.assertEqual(self.conn.pushed[0]["chat_type"], 2)

    async def test_typewriter_failure_pushes_with_chatid(self):
        """流式刷新失败时必须带着 chatid 退回主动推送，不能静默丢消息。"""
        gateway = self.build(
            typewriter_chunk_chars=10,
            typewriter_interval_ms=500,
            stream_soft_deadline_seconds=10_000_000_000,
        )
        self.conn = BrokenStreamConnection()
        gateway._connection = self.conn
        self.store.save_stream(
            "t-4",
            {
                "req_id": "req-4",
                "stream_id": "s-4",
                "chatid": "chat-4",
                "chattype": "group",
                "started_at": 0,
            },
        )
        reply = {
            "task_id": "t-4",
            "req_id": "req-4",
            "stream_id": "s-4",
            "chatid": "chat-4",
            "chattype": "group",
            "ok": True,
            "answer": "完整答案",
            "started_at": 0,
        }
        await gateway._deliver(reply)

        self.assertEqual(len(self.conn.pushed), 1)
        self.assertEqual(self.conn.pushed[0]["chatid"], "chat-4")
        self.assertEqual(self.conn.pushed[0]["content"], "完整答案")
        self.assertEqual(self.conn.pushed[0]["chat_type"], 2)

    async def test_long_answer_ends_stream_before_pushing(self):
        """超过 markdown 字节上限的答案必须先结束流式消息，再走主动推送。"""
        gateway = self.build(stream_soft_deadline_seconds=10_000_000_000)
        self.store.save_stream(
            "t-5",
            {"req_id": "req-5", "stream_id": "s-5", "chatid": "chat-5", "chattype": "group"},
        )
        answer = "中" * 9000
        reply = {
            "task_id": "t-5",
            "req_id": "req-5",
            "stream_id": "s-5",
            "chatid": "chat-5",
            "chattype": "group",
            "ok": True,
            "answer": answer,
            "started_at": 0,
        }
        await gateway._deliver(reply)

        self.assertTrue(self.conn.streams[-1]["finish"])
        self.assertIn("答案较长", self.conn.streams[-1]["content"])
        self.assertEqual(self.conn.pushed[0]["content"], answer)

    async def test_deliver_failure_reports_reason(self):
        """失败结果要把原因写给用户，而不是空消息。"""
        gateway = self.build()
        self.store.save_stream("t-3", {"req_id": "req-3", "stream_id": "s-3", "chatid": "chat-3"})
        reply = {
            "task_id": "t-3",
            "req_id": "req-3",
            "stream_id": "s-3",
            "chatid": "chat-3",
            "chattype": "group",
            "ok": False,
            "answer": "",
            "error": "拉取 yicall-common 失败",
            "started_at": 0,
        }
        await gateway._deliver(reply)
        self.assertIn("分析未完成", self.conn.streams[-1]["content"])
        self.assertIn("拉取 yicall-common 失败", self.conn.streams[-1]["content"])

    async def test_deliver_skips_unknown_task(self):
        """上下文已被删除时不再重复推送。"""
        gateway = self.build()
        await gateway._deliver({"task_id": "missing", "req_id": "r", "ok": True, "answer": "x"})
        self.assertEqual(self.conn.streams, [])
        self.assertEqual(self.conn.pushed, [])


class RoutesTest(unittest.TestCase):
    """校验机器人只读接口已注册且能返回配置内容。"""

    @classmethod
    def setUpClass(cls):
        """构造使用内存库并跳过登录校验的测试应用。"""
        from tests.test_skill_routes import create_test_app

        cls.app = create_test_app()

    def test_projects_endpoint_returns_all_projects(self):
        """接口应返回 Excel 里的 5 个项目及其模块分支。"""
        response = self.app.test_client().get("/chatbot/projects")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["total"], 5)
        first = payload["items"][0]
        self.assertEqual(first["name"], "call3.1主线")
        self.assertEqual(len(first["modules"]), 10)

    def test_health_endpoint_reports_config_state(self):
        """健康检查应给出项目数与机器人凭证状态。"""
        response = self.app.test_client().get("/chatbot/health")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["project_count"], 5)
        self.assertIn("gateway", payload)

    def test_create_app_publishes_redis_client_to_extensions(self):
        """网关与 chatbot worker 从 extensions.redis 取连接，create_app 必须写回该模块。"""
        import extensions

        self.assertIsNotNone(extensions.redis)

    def test_chatbot_answer_task_is_registered(self):
        """装配 Flask 应用后 Celery 必须能识别 chatbot.answer，否则 worker 拒收任务。"""
        from extensions import celery

        self.assertIn("chatbot.answer", celery.tasks)


if __name__ == "__main__":
    unittest.main()
