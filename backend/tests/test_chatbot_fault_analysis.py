"""故障分析意图、项目识别、模块筛选与提示词契约测试。"""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class FaultIntentTests(unittest.TestCase):
    def setUp(self):
        from app.chatbot.settings import load_settings

        self.settings = load_settings()

    def test_fault_terms_trigger_but_document_questions_do_not(self):
        from app.chatbot.fault_analysis import is_fault_analysis_question

        for question in (
            "bot 登录一直报超时，帮我分析根因",
            "这个接口返回 500 异常",
            "页面打开后空白，麻烦排查",
            "服务启动失败",
        ):
            self.assertTrue(is_fault_analysis_question(question), question)

        for question in (
            "call3.1 最新部署文档在哪里",
            "招商证券环境的域名是什么",
            "登录账号是什么",
        ):
            self.assertFalse(is_fault_analysis_question(question), question)

    def test_project_is_matched_from_alias_and_module(self):
        from app.chatbot.fault_analysis import match_project

        self.assertEqual(
            match_project(self.settings, "招商证券登录报错，帮我定位").key,
            "chatcall-3.1-zszq",
        )
        self.assertEqual(
            match_project(self.settings, "call3.1主线结果回调超时").key,
            "chatcall-3.1-main",
        )
        self.assertEqual(
            match_project(self.settings, "see-task 执行失败，帮我定位").key,
            "chatsee-main",
        )
        self.assertEqual(
            match_project(self.settings, "pal-quality-inspection 质检超时").key,
            "chatpal-main",
        )

    def test_normalize_removes_whitespace_without_dropping_letters(self):
        """文本归一化只去掉空白，不能因为正则写错误删字母 s。"""
        from app.chatbot.fault_analysis import _normalize

        self.assertEqual(_normalize("  Call 3.1 S  "), "call3.1s")

    def test_ambiguous_module_only_question_does_not_pick_a_project(self):
        from app.chatbot.fault_analysis import match_project

        # aiforce2 同时属于多个项目，不能猜测，必须让用户选择。
        self.assertIsNone(match_project(self.settings, "aiforce2 登录报错"))

    def test_explicit_module_does_not_pad_with_unrelated_modules(self):
        """问题只点名一个模块时，不应为了凑上限再拉取无关模块。"""
        from app.chatbot.fault_analysis import select_modules

        project = self.settings.project_by_key("chatcall-3.1-main")
        selected = select_modules(project, "aiforce-web 页面报错，登录超时", max_modules=4)

        self.assertEqual([module for module, _branch in selected], ["aiforce-web"])

    def test_module_selection_prefers_mentioned_module_and_limits_count(self):
        from app.chatbot.fault_analysis import select_modules

        project = self.settings.project_by_key("chatcall-3.1-main")
        selected = select_modules(project, "aiforce-web 页面报错，登录超时", max_modules=2)

        self.assertLessEqual(len(selected), 2)
        self.assertEqual(selected[0][0], "aiforce-web")
        self.assertIn(selected[0][0], {item for item, _branch in project.module_items()})


class FaultRoutingTests(unittest.TestCase):
    def test_fault_mode_uses_code_worker_even_when_knowledge_is_ready(self):
        """故障分析必须进入代码 worker，不能被 Confluence 知识队列截走。"""
        from chatbot_gateway import select_answer_task

        settings = SimpleNamespace(
            knowledge=SimpleNamespace(enabled=True),
            confluence=SimpleNamespace(configured=True),
        )

        self.assertEqual(
            select_answer_task(settings, "fault"),
            ("chatbot.answer", "chatbot"),
        )


class FaultPromptTests(unittest.TestCase):
    def test_fault_prompt_contains_evidence_and_verification_contract(self):
        from app.chatbot.answer import build_prompt
        from app.chatbot.settings import load_settings

        settings = load_settings()
        project = settings.project_by_key("chatcall-3.1-main")
        workspace = SimpleNamespace(
            available=lambda: [
                SimpleNamespace(module="aiforce2", branch="release/2.7"),
            ],
            failed=lambda: [],
        )

        prompt = build_prompt(
            "登录一直超时报错，帮我分析根因",
            project,
            workspace,
            settings.answer.max_answer_chars,
            mode="fault",
            background="[S1] 故障排查手册\n超时先检查网关配置。[S1]",
        )

        self.assertIn("故障分析", prompt)
        self.assertIn("可能原因", prompt)
        self.assertIn("代码定位", prompt)
        self.assertIn("建议验证", prompt)
        self.assertIn("aiforce2", prompt)
        self.assertIn("Confluence 背景资料", prompt)
        self.assertIn("与代码冲突时以代码为准", prompt)


class FaultTaskTests(unittest.TestCase):
    def test_answer_task_selects_modules_and_runs_fault_mode(self):
        """故障任务层必须使用筛选后的模块，并把 fault 模式传给回答生成器。"""
        from flask import Flask
        from unittest.mock import MagicMock, patch
        from app.chatbot import tasks

        app = Flask(__name__)
        project = SimpleNamespace(
            key="chatcall-3.1-main",
            name="ChatCall 3.1 主线",
            module_items=lambda: [
                ("aiforce-web", "release/v2.7"),
                ("yicall-task", "release/3.1"),
            ],
        )
        settings = SimpleNamespace(
            gateway=SimpleNamespace(reply_channel="chatbot:reply"),
            fault_analysis=SimpleNamespace(max_modules=1),
            answer=SimpleNamespace(max_answer_chars=1200),
            project_by_key=lambda key: project if key == project.key else None,
        )
        selected = [("aiforce-web", "release/v2.7")]
        workspace = SimpleNamespace(cleanup=MagicMock())
        answer_result = {
            "ok": True,
            "answer": "结论",
            "error": "",
            "duration_ms": 12,
            "usage": {},
        }
        payload = {
            "task_id": "cb-fault-task",
            "project_key": project.key,
            "project_name": project.name,
            "question": "aiforce-web 登录一直超时，帮我分析根因",
            "answer_mode": "fault",
        }

        with tempfile.TemporaryDirectory() as workspace_root:
            app.config["SKILL_WORKSPACE_DIR"] = workspace_root
            with app.app_context(), patch.object(tasks, "load_settings", return_value=settings), \
                    patch.object(tasks, "_upsert_record", return_value=None), \
                    patch.object(tasks, "_codex_options", return_value=SimpleNamespace()), \
                    patch.object(tasks.fault_analysis, "select_modules", return_value=selected) as select, \
                    patch.object(tasks, "_fault_background", return_value="背景资料"), \
                    patch.object(tasks.answer, "prepare_workspace", return_value=workspace) as prepare, \
                    patch.object(tasks.answer, "run_answer", return_value=answer_result) as run, \
                    patch.object(tasks, "publish_result") as publish:
                result = tasks.answer_task.run(payload)

        self.assertTrue(result["ok"])
        select.assert_called_once_with(
            project,
            payload["question"],
            settings.fault_analysis.max_modules,
        )
        prepare.assert_called_once()
        self.assertEqual(prepare.call_args.kwargs["modules"], selected)
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["mode"], "fault")
        self.assertEqual(run.call_args.kwargs["background"], "背景资料")
        workspace.cleanup.assert_called_once_with(settings)
        publish.assert_called_once()


if __name__ == "__main__":
    unittest.main()
