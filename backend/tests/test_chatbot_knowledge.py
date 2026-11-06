"""Confluence 知识库接入机器人的核心契约测试。"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class KnowledgeRoutingTests(unittest.TestCase):
    def test_gateway_uses_knowledge_queue_when_confluence_is_ready(self):
        """知识库可用时，网关必须把问题投递到知识问答队列。"""
        from chatbot_gateway import select_answer_task

        settings = SimpleNamespace(
            knowledge=SimpleNamespace(enabled=True, embedding_model=""),
            confluence=SimpleNamespace(configured=True),
        )

        self.assertEqual(
            select_answer_task(settings),
            ("chatbot.knowledge_answer", "chatbot-knowledge"),
        )

    def test_gateway_falls_back_to_code_answer_when_confluence_is_disabled(self):
        """未启用 Confluence 时保留现有代码问答链路。"""
        from chatbot_gateway import select_answer_task

        settings = SimpleNamespace(
            knowledge=SimpleNamespace(enabled=False),
            confluence=SimpleNamespace(configured=False),
        )

        self.assertEqual(select_answer_task(settings), ("chatbot.answer", "chatbot"))


class KnowledgeTaskRegistrationTests(unittest.TestCase):
    def test_sync_and_answer_tasks_are_registered(self):
        """worker 导入后必须能按任务名找到同步与知识问答任务。"""
        from app.chatbot import tasks

        registered = set(tasks.celery.tasks)

        self.assertIn("knowledge.sync_confluence", registered)
        self.assertIn("chatbot.knowledge_answer", registered)


class KnowledgeTaskWithoutProjectTests(unittest.TestCase):
    def test_knowledge_answer_runs_without_a_code_project(self):
        """Confluence 知识问答必须允许 project=None，不能要求用户先选代码项目。"""
        from flask import Flask
        from app.chatbot import tasks

        app = Flask(__name__)
        settings = SimpleNamespace(
            gateway=SimpleNamespace(reply_channel="chatbot:reply"),
            project_by_key=lambda _key: None,
            knowledge=SimpleNamespace(enabled=True, max_total_chars=50000),
            confluence=SimpleNamespace(configured=True),
            answer=SimpleNamespace(max_answer_chars=800),
        )
        hit = SimpleNamespace()
        answer_result = SimpleNamespace(
            ok=True,
            answer="先检查权限。[S1]",
            error="",
            duration_ms=12,
            usage={},
        )
        payload = {
            "task_id": "cb-knowledge-no-project",
            "project_key": "",
            "project_name": "Confluence 知识库",
            "question": "权限怎么检查",
            "strict_citation": True,
        }

        with tempfile.TemporaryDirectory() as workspace:
            app.config["SKILL_WORKSPACE_DIR"] = workspace
            with app.app_context(), patch.object(tasks, "load_settings", return_value=settings), \
                    patch.object(tasks, "_upsert_record", return_value=None), \
                    patch.object(tasks, "_codex_options", return_value=SimpleNamespace()), \
                    patch.object(
                        tasks.knowledge,
                        "retrieve_hits",
                        side_effect=[[hit], [hit], [hit]],
                    ) as retrieve, \
                    patch.object(tasks.knowledge, "direct_confluence_search", return_value=[999]) as online, \
                    patch.object(tasks.knowledge, "refresh_hits_live", return_value=[1]), \
                    patch.object(tasks.knowledge_answer, "run_knowledge_answer", return_value=answer_result) as answer_run, \
                    patch.object(tasks, "_save_answer_sources"), \
                    patch.object(tasks, "_publish_reply", return_value=True) as publish:
                result = tasks.knowledge_answer_task.run(payload)

        self.assertTrue(result["ok"])
        self.assertIsNone(retrieve.call_args_list[0].args[1])
        online.assert_called_once_with("权限怎么检查", settings)
        self.assertEqual(retrieve.call_args_list[1].kwargs["document_ids"], [999])
        self.assertEqual(publish.call_args.args[1]["answer"], "先检查权限。[S1]")
        self.assertTrue(answer_run.call_args.kwargs["require_citation"])


class KnowledgeRankingTests(unittest.TestCase):
    def test_query_intent_is_extracted_for_different_products_and_actions(self):
        """产品问答必须按语义提取产品、动作、文档类型和最新要求。"""
        try:
            from app.chatbot.knowledge import infer_query_intent
        except ModuleNotFoundError as exc:
            self.skipTest(f"backend dependencies missing: {exc}")

        rollback = infer_query_intent("PAL 怎么回滚")
        deployment = infer_query_intent("YIC 最新部署指引在哪里")
        handbook = infer_query_intent("SEE 最新版手册")
        environment = infer_query_intent("demo环境 bot 的登录信息是什么")

        self.assertEqual(rollback.product_terms, ("pal",))
        self.assertIn("回滚", rollback.action_terms)
        self.assertEqual(deployment.product_terms, ("yic",))
        self.assertIn("部署", deployment.action_terms)
        self.assertIn("部署指引", deployment.document_terms)
        self.assertTrue(deployment.wants_latest)
        self.assertEqual(handbook.product_terms, ("see",))
        self.assertIn("手册", handbook.document_terms)
        self.assertTrue(handbook.wants_latest)
        self.assertEqual(environment.product_terms, ("demo", "bot"))
        self.assertIn("登录信息", environment.information_terms)
        self.assertIn("环境", environment.action_terms)
        self.assertTrue(environment.wants_reference)

    def test_environment_information_summary_beats_old_incident_record(self):
        """查询环境登录信息时，汇总页必须胜过只提到登录地址的历史故障页。"""
        try:
            from flask import Flask
            from extensions import db
            from app.chatbot.knowledge import retrieve_hits
            from app.chatbot.models.knowledge import KnowledgeChunk, KnowledgeDocument
        except ModuleNotFoundError as exc:
            self.skipTest(f"backend dependencies missing: {exc}")

        app = Flask(__name__)
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        db.init_app(app)
        with app.app_context():
            db.create_all()
            rows = [
                (
                    "1",
                    "Demo环境bot“内部使用”web页面无法访问",
                    datetime(2023, 6, 30),
                    "Demo环境bot登录页面无法访问，请使用 http://example.test/#/user/login 进入。",
                ),
                (
                    "2",
                    "Poc&Demo环境信息汇总",
                    datetime(2026, 10, 23),
                    "bot3.2 环境 namespace 为 bot-310。服务器登录账密：ops / first@2019--。"
                    "bot-portal 地址为 bot-bot-portal.example.test。",
                ),
                (
                    "3",
                    "BOT生产环境信息",
                    datetime(2026, 3, 26),
                    "生产环境登录账号和密码由客户管理员提供。",
                ),
                (
                    "4",
                    "BOT3.1 DD环境信息",
                    datetime(2026, 3, 26),
                    "SecureCRT 账号：root 密码：example-password 端口：16022。",
                ),
                (
                    "5",
                    "Bot公有云环境信息（旧）",
                    datetime(2021, 4, 8),
                    "bot250-demo 环境账号：old-user 密码：old-password。",
                ),
            ]
            for source_id, title, updated_at, content in rows:
                document = KnowledgeDocument(
                    source_id=source_id,
                    title=title,
                    space_key="opsDocs",
                    url=f"https://confluence.example/pages/{source_id}",
                    current_version=1,
                    updated_at_source=updated_at,
                    content=content,
                    content_hash=f"hash-{source_id}",
                    status="active",
                    last_sync_run_id="test",
                )
                db.session.add(document)
                db.session.flush()
                db.session.add(
                    KnowledgeChunk(
                        document_id=document.id,
                        chunk_index=0,
                        content=content,
                        token_count=10,
                        content_hash=f"chunk-{source_id}",
                    )
                )
            db.session.commit()
            settings = SimpleNamespace(
                knowledge=SimpleNamespace(
                    max_chunks=3,
                    space_keys=(),
                    embedding_model="",
                    min_score=0,
                )
            )

            hits = retrieve_hits("demo环境 bot 的登录信息是什么", None, settings)

        self.assertTrue(hits)
        self.assertEqual(hits[0].document.title, "Poc&Demo环境信息汇总")

    def test_reference_query_prefers_complete_table_chunks_not_continuations(self):
        """登录信息查询应同时选到字段表头和对应环境行，而不是表格续片。"""
        try:
            from flask import Flask
            from extensions import db
            from app.chatbot.knowledge import retrieve_hits
            from app.chatbot.models.knowledge import KnowledgeChunk, KnowledgeDocument
        except ModuleNotFoundError as exc:
            self.skipTest(f"backend dependencies missing: {exc}")

        app = Flask(__name__)
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        db.init_app(app)
        with app.app_context():
            db.create_all()
            document = KnowledgeDocument(
                source_id="10",
                title="Poc&Demo环境信息汇总",
                space_key="opsDocs",
                url="https://confluence.example/pages/10",
                current_version=1,
                updated_at_source=datetime(2026, 10, 23),
                content="environment reference",
                content_hash="hash-10",
                status="active",
                last_sync_run_id="test",
            )
            db.session.add(document)
            db.session.flush()
            chunks = [
                "| 服务器节点名称 | role | IP | 登录账密 |\n| k8s-master | master | 10.0.0.1 | ops / first@2019-- |",
                "| 环境 | namespace | 域名 |\n| bot3.2 | bot-310 | bot-portal: bot-bot-portal.example.test |",
                "| pal1.11 | pal-shanghai | 备注：对接bot，对接bot-bot-portal.example.test环境 |",
            ]
            for index, content in enumerate(chunks):
                db.session.add(
                    KnowledgeChunk(
                        document_id=document.id,
                        chunk_index=index,
                        content=content,
                        token_count=20,
                        content_hash=f"chunk-10-{index}",
                    )
                )
            db.session.commit()
            settings = SimpleNamespace(
                knowledge=SimpleNamespace(
                    max_chunks=3,
                    space_keys=(),
                    embedding_model="",
                    min_score=0,
                )
            )

            hits = retrieve_hits("demo环境 bot 的登录信息是什么", None, settings)

        selected_text = "\n".join(hit.chunk.content for hit in hits)
        self.assertIn("bot-310", selected_text)
        self.assertIn("ops / first@2019--", selected_text)
        self.assertNotIn("pal-shanghai", selected_text)

    def test_latest_guide_beats_old_versions_and_recent_unrelated_pages(self):
        """询问最新部署文档时，文档意图必须胜过旧版本号和无关的近期页面。"""
        try:
            from flask import Flask
            from extensions import db
            from app.chatbot.knowledge import retrieve_hits
            from app.chatbot.models.knowledge import KnowledgeChunk, KnowledgeDocument
        except ModuleNotFoundError as exc:
            self.skipTest(f"backend dependencies missing: {exc}")

        app = Flask(__name__)
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        db.init_app(app)
        with app.app_context():
            db.create_all()
            rows = [
                ("1", "Call一键部署指引--3.0.0(K8S)", "Call 最新部署步骤、环境准备和发布操作。"),
                ("2", "Call值周", "Call 问题目录和周会记录。"),
                ("3", "中文强化模型回滚后状态未流转", "最新文档记录了回滚后的状态。"),
                ("4", "Pal1.9一键部署文档(K8S)", "Pal 产品部署文档，正文也提到 call 集成。"),
                ("5", "Call一键部署指引--2.3.0&2.4.0", "Call 部署文档旧版本。"),
                (
                    "6",
                    "call3.x+版本一键部署指引-csyh（长沙银行）",
                    "1 最新文档。当前有效版本，完整部署步骤见附件。",
                ),
                (
                    "7",
                    "Call现场部署版本信息",
                    "Call 现场部署版本清单，近期更新，但不是部署文档。",
                ),
                (
                    "8",
                    "Call数据迁移恢复文档",
                    "Call 数据迁移与恢复记录，近期更新，不包含部署步骤。",
                ),
            ]
            for source_id, title, content in rows:
                document = KnowledgeDocument(
                    source_id=source_id,
                    title=title,
                    space_key="CallKnowledgeBase",
                    url=f"https://confluence.example/pages/{source_id}",
                    current_version=1,
                    updated_at_source=datetime(2026, 1, int(source_id)),
                    content=content,
                    content_hash=f"hash-{source_id}",
                    status="active",
                    last_sync_run_id="test",
                )
                db.session.add(document)
                db.session.flush()
                db.session.add(
                    KnowledgeChunk(
                        document_id=document.id,
                        chunk_index=0,
                        content=content,
                        token_count=5,
                        content_hash=f"chunk-{source_id}",
                    )
                )
            db.session.commit()
            settings = SimpleNamespace(
                knowledge=SimpleNamespace(
                    max_chunks=3,
                    space_keys=(),
                    embedding_model="",
                    min_score=0,
                )
            )

            hits = retrieve_hits(
                "我现在想部署一个call  call 最新的部署文档在哪里",
                None,
                settings,
            )

        self.assertTrue(hits)
        self.assertEqual(hits[0].document.title, "call3.x+版本一键部署指引-csyh（长沙银行）")


class KnowledgeAdminRouteTests(unittest.TestCase):
    def _routes(self):
        from flask import Flask
        from app.chatbot.routes import chatbot_bp

        app = Flask(__name__)
        app.register_blueprint(chatbot_bp, url_prefix="/chatbot")
        return app

    def test_status_and_manual_sync_routes_exist(self):
        """运维必须能查看索引状态并手动触发全量或单页同步。"""
        rules = {rule.rule for rule in self._routes().url_map.iter_rules()}

        self.assertIn("/chatbot/knowledge/status", rules)
        self.assertIn("/chatbot/knowledge/sync", rules)
        self.assertIn("/chatbot/knowledge/pages/<page_id>/sync", rules)

    def test_manual_sync_enqueues_dedicated_knowledge_queue(self):
        """手动同步接口不能在 API 进程内执行，必须投递到知识同步队列。"""
        from app.chatbot import routes

        app = self._routes()
        with app.test_request_context("/chatbot/knowledge/sync", method="POST"):
            with patch.object(routes.tasks.knowledge_sync_task, "apply_async") as apply_async:
                response, status = routes.sync_knowledge()

        self.assertEqual(status, 202)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(apply_async.call_args.kwargs["queue"], "knowledge-sync")


class KnowledgeMigrationTests(unittest.TestCase):
    def test_confluence_knowledge_migration_declares_all_tables(self):
        """迁移必须创建文档、切片、同步批次和回答来源四张表。"""
        migration_path = (
            BACKEND_DIR / "migrations" / "versions" / "f6e3b9d24c87_confluence_knowledge.py"
        )
        self.assertTrue(migration_path.is_file())
        spec = importlib.util.spec_from_file_location("knowledge_migration", migration_path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        self.assertEqual(migration.revision, "f6e3b9d24c87")
        self.assertEqual(migration.down_revision, "e5d2a8c14b73")

    def test_bootstrap_schema_contains_all_knowledge_tables(self):
        """在不使用 Alembic 的兜底建库路径中也要创建完整知识库结构。"""
        import bootstrap_schema

        ddl = "\n".join(bootstrap_schema.DDL_STATEMENTS).lower()
        for table in (
            "knowledge_documents",
            "knowledge_chunks",
            "knowledge_sync_runs",
            "chatbot_answer_sources",
        ):
            self.assertIn(f"create table if not exists {table}", ddl)


class KnowledgeConfigurationTests(unittest.TestCase):
    def test_chatbot_config_declares_opt_in_confluence_knowledge(self):
        """项目映射文件必须显式提供 Confluence 与知识库参数，且默认关闭。"""
        path = BACKEND_DIR / "config" / "chatbot.json"
        document = json.loads(path.read_text(encoding="utf-8"))

        self.assertIn("confluence", document)
        self.assertIn("knowledge", document)
        self.assertFalse(document["knowledge"]["enabled"])
        self.assertEqual(document["knowledge"]["sync_interval_seconds"], 21600)

    def test_environment_can_enable_knowledge_without_exposing_credentials(self):
        """部署环境必须能通过环境变量启用知识库，凭据仍不进入 JSON。"""
        from app.chatbot.settings import load_settings

        with patch.dict(
            os.environ,
            {
                "KNOWLEDGE_ENABLED": "true",
                "CONFLUENCE_URL": "https://confluence.example",
                "CONFLUENCE_AUTH_MODE": "bearer",
                "CONFLUENCE_TOKEN": "read-only-token",
                "KNOWLEDGE_SPACE_KEYS": "OPS,PRODUCT",
                "KNOWLEDGE_SYNC_PAGE_LIMIT": "1200",
            },
            clear=False,
        ):
            settings = load_settings()

        self.assertTrue(settings.knowledge.enabled)
        self.assertTrue(settings.confluence.configured)
        self.assertEqual(settings.knowledge.space_keys, ("OPS", "PRODUCT"))
        self.assertEqual(settings.knowledge.sync_page_limit, 1200)


class KnowledgePersistenceTests(unittest.TestCase):
    def test_page_upsert_is_idempotent_and_rebuilds_changed_chunks(self):
        """同一页面重复同步只保留一套切片，内容变化时重建切片。"""
        try:
            from flask import Flask
            from extensions import db
            from app.chatbot.confluence import ConfluencePage
            from app.chatbot.knowledge import upsert_page
            from app.chatbot.models.knowledge import KnowledgeChunk, KnowledgeDocument
        except ModuleNotFoundError as exc:
            self.skipTest(f"backend dependencies missing: {exc}")

        app = Flask(__name__)
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        db.init_app(app)
        settings = SimpleNamespace(
            max_chunk_chars=200,
            chunk_overlap_chars=20,
            embedding_model="",
            embedding_batch_size=4,
        )
        with app.app_context():
            db.create_all()
            page = ConfluencePage(
                page_id="101",
                title="部署说明",
                space_key="OPS",
                url="https://confluence.example/pages/101",
                version=1,
                updated_at=None,
                labels=("runbook",),
                body_html="<p>正文</p>",
                content="部署说明\n第一步启动服务\n第二步检查健康状态",
            )

            self.assertTrue(upsert_page(page, "run-1", settings))
            db.session.commit()
            first_count = KnowledgeChunk.query.count()
            self.assertGreater(first_count, 0)
            self.assertFalse(upsert_page(page, "run-2", settings))
            db.session.commit()

            changed = ConfluencePage(
                **{
                    **page.__dict__,
                    "version": 2,
                    "content": "部署说明\n步骤已经更新为滚动发布",
                }
            )
            self.assertTrue(upsert_page(changed, "run-3", settings))
            db.session.commit()

            self.assertEqual(KnowledgeDocument.query.count(), 1)
            self.assertEqual(KnowledgeDocument.query.one().current_version, 2)
            self.assertEqual(KnowledgeChunk.query.count(), KnowledgeChunk.query.filter_by(document_id=1).count())


class SingleAnswerTests(unittest.TestCase):
    def test_prompt_requires_concrete_login_values_and_scope(self):
        """登录信息问题必须要求模型给出具体值及其作用域。"""
        from app.chatbot.knowledge_answer import build_prompt

        hit = SimpleNamespace(
            document=SimpleNamespace(
                title="Poc&Demo环境信息汇总",
                source_id="2",
                current_version=1,
                updated_at_source=datetime(2026, 10, 23),
                url="https://confluence.example/pages/2",
            ),
            chunk=SimpleNamespace(
                heading_path="Kubernetes服务器信息",
                content="bot3.2 namespace：bot-310。登录账密：ops / first@2019--",
            ),
        )

        prompt = build_prompt("demo环境 bot 的登录信息是什么", "", [hit], 800, 50000)

        self.assertIn("登录账密", prompt)
        self.assertIn("具体值及其作用域", prompt)
        self.assertIn("不要以“只提供了服务器登录”为由回答未找到", prompt)

    def test_structured_model_output_is_reduced_to_one_conclusion(self):
        """即使模型返回多段候选，也只保留一个答案、一句依据和一个来源。"""
        from app.chatbot.knowledge_answer import ensure_single_answer

        hit = SimpleNamespace(
            document=SimpleNamespace(
                title="发布流程",
                current_version=7,
                url="https://confluence.example/pages/7",
            ),
        )
        answer = """答案：先灰度发布再全量发布。[S1]
来源：[S1] 发布流程
链接：https://confluence.example/pages/7

备选答案：也可以直接全量发布。[S1]
"""

        normalized = ensure_single_answer(answer, [hit], 800)

        self.assertEqual(normalized.count("答案："), 1)
        self.assertEqual(normalized.count("来源："), 1)
        self.assertEqual(normalized.count("链接："), 1)
        self.assertNotIn("页面ID", normalized)
        self.assertNotIn("版本85", normalized)
        self.assertNotIn("备选答案", normalized)

    def test_answer_without_citation_gets_a_confluence_source(self):
        """模型漏写引用时必须补上唯一来源，不能返回无依据答案。"""
        from app.chatbot.knowledge_answer import ensure_single_answer

        hit = SimpleNamespace(
            document=SimpleNamespace(
                title="权限说明",
                current_version=3,
                url="https://confluence.example/pages/3",
            ),
        )

        normalized = ensure_single_answer("先检查账号权限。", [hit], 800)

        self.assertIn("[S1]", normalized)
        self.assertIn("权限说明", normalized)
        self.assertIn("https://confluence.example/pages/3", normalized)


    def test_non_first_citation_does_not_get_duplicate_source(self):
        """A non-S1 citation must not trigger the fallback source."""
        from app.chatbot.knowledge_answer import ensure_single_answer

        hit = SimpleNamespace(
            document=SimpleNamespace(title="Problem index", current_version=4),
        )
        answer = "Answer [S3]"

        normalized = ensure_single_answer(answer, [hit], 800)

        self.assertEqual(normalized.count("[S3]"), 1)
        self.assertNotIn("[S1]", normalized)


class KnowledgeCitationTests(unittest.TestCase):
    def test_citation_must_reference_an_evidence_item_from_this_turn(self):
        """严格模式下只接受本轮证据范围内、且实际存在的引用。"""
        from app.chatbot.knowledge_answer import has_valid_citation

        hits = [object(), object()]
        self.assertTrue(has_valid_citation("结论 [S1]", hits))
        self.assertTrue(has_valid_citation("结论 [S1][S2]", hits))
        self.assertFalse(has_valid_citation("结论 [S3]", hits))
        self.assertFalse(has_valid_citation("没有引用的结论", hits))

    def test_strict_mode_rejects_an_answer_without_a_valid_citation(self):
        """普通问题走知识库时，模型漏引用必须返回证据不足，不能补一个假来源。"""
        from app.chatbot.answer import CodexOptions
        from app.chatbot import knowledge_answer

        hit = SimpleNamespace(
            document=SimpleNamespace(
                title="权限说明",
                source_id="100",
                current_version=3,
                updated_at_source=datetime(2026, 1, 1),
                url="https://confluence.example/pages/3",
            ),
            chunk=SimpleNamespace(heading_path="权限", content="先检查账号权限。"),
        )
        result = SimpleNamespace(
            last_message="先检查账号权限。",
            network_failed=False,
            timed_out=False,
            exit_code=0,
            error="",
            usage={},
        )
        with tempfile.TemporaryDirectory() as workspace, patch.object(
            knowledge_answer.codex_runner, "run_codex_skill", return_value=result
        ):
            answer = knowledge_answer.run_knowledge_answer(
                "权限怎么检查",
                None,
                [hit],
                CodexOptions(),
                workspace,
                require_citation=True,
            )

        self.assertFalse(answer.ok)
        self.assertIn("证据不足", answer.error)
        self.assertEqual(answer.answer, "")

    def test_insufficient_answer_is_not_treated_as_missing_citation(self):
        """模型明确回答证据不足时，不应再报“缺少引用”。"""
        from app.chatbot.knowledge_answer import _is_insufficient_answer

        self.assertTrue(_is_insufficient_answer("Confluence 中未找到能够回答该问题的依据"))
        self.assertFalse(_is_insufficient_answer("先检查账号权限 [S1]"))


if __name__ == "__main__":
    unittest.main()
