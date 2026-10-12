import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock


BACKEND_DIR = Path(__file__).resolve().parents[1]
ROUTES_DIR = BACKEND_DIR / "app" / "analysis" / "routes"
ROUTES_PATH = ROUTES_DIR / "routes.py"
AI_OPTIONS_PATH = BACKEND_DIR / "app" / "ai_options.py"


class BlueprintStub:
    def __init__(self, *args, **kwargs):
        pass

    def route(self, *args, **kwargs):
        return lambda func: func


class FakeTaskResult:
    id = "task-123"
    state = "PENDING"


class FakeAnalyzeTask:
    def __init__(self):
        self.submitted = []

    def delay(self, data):
        self.submitted.append(data)
        return FakeTaskResult()


class FakeAiResponse:
    status_code = 429

    def json(self):
        return {
            "error": {
                "message": "The usage limit has been reached",
                "type": "usage_limit_reached",
            }
        }


class FakeAsyncResult:
    info = {}

    def __init__(self, task_id, app=None):
        self.id = task_id
        self.state = "SUCCESS"
        self.result = {"code_analysis": "done"}
        self.info = self.__class__.info

    def ready(self):
        return True

    def successful(self):
        return True


def install_route_stubs():
    ai_options_spec = importlib.util.spec_from_file_location("app.ai_options", AI_OPTIONS_PATH)
    ai_options_module = importlib.util.module_from_spec(ai_options_spec)
    sys.modules["app.ai_options"] = ai_options_module
    ai_options_spec.loader.exec_module(ai_options_module)

    class FakeChatCompletions:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            message = types.SimpleNamespace(content="chat-result")
            choice = types.SimpleNamespace(message=message)
            return types.SimpleNamespace(choices=[choice])

    class FakeResponses:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return types.SimpleNamespace(output_text="responses-result")

    class FakeOpenAI:
        last_instance = None

        def __init__(self, api_key=None, base_url=None):
            self.api_key = api_key
            self.base_url = base_url
            self.chat = types.SimpleNamespace(completions=FakeChatCompletions())
            self.responses = FakeResponses()
            FakeOpenAI.last_instance = self

    flask_stub = types.ModuleType("flask")
    flask_stub.Blueprint = BlueprintStub
    flask_stub.request = types.SimpleNamespace(json={})
    flask_stub.jsonify = lambda payload: payload
    flask_stub.current_app = types.SimpleNamespace(config={}, logger=types.SimpleNamespace(
        info=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    ))
    sys.modules["flask"] = flask_stub

    celery_result_stub = types.ModuleType("celery.result")
    celery_result_stub.AsyncResult = FakeAsyncResult
    sys.modules["celery.result"] = celery_result_stub

    openai_stub = types.ModuleType("openai")
    openai_stub.OpenAI = FakeOpenAI
    sys.modules["openai"] = openai_stub

    class FakeCeleryControl:
        def __init__(self):
            self.revoked = []

        def revoke(self, task_id, terminate=False, signal=None):
            self.revoked.append({
                "task_id": task_id,
                "terminate": terminate,
                "signal": signal,
            })

    fake_celery = types.SimpleNamespace(control=FakeCeleryControl())

    extensions_stub = types.ModuleType("extensions")
    extensions_stub.celery = fake_celery
    extensions_stub.db = types.SimpleNamespace(session=types.SimpleNamespace(
        add=lambda *args, **kwargs: None,
        commit=lambda *args, **kwargs: None,
        rollback=lambda *args, **kwargs: None,
    ))
    sys.modules["extensions"] = extensions_stub

    for name in ["app", "app.analysis", "app.analysis.routes", "app.logfile", "app.logfile.models"]:
        module = types.ModuleType(name)
        module.__path__ = [str(ROUTES_DIR)] if name == "app.analysis.routes" else []
        sys.modules[name] = module

    sys.modules["app"].db = types.SimpleNamespace(session=types.SimpleNamespace(
        add=lambda *args, **kwargs: None,
        commit=lambda *args, **kwargs: None,
        rollback=lambda *args, **kwargs: None,
    ))

    logfile_model_stub = types.ModuleType("app.logfile.models.model")
    logfile_model_stub.QueryRecord = lambda **kwargs: kwargs
    logfile_model_stub.AnalysisKnowledgeCase = types.SimpleNamespace
    logfile_model_stub.Log = types.SimpleNamespace
    sys.modules["app.logfile.models.model"] = logfile_model_stub

    config_stub = types.ModuleType("app.config")
    config_stub.DEFAULT_REPO_CACHE_DIR = "/tmp/repo-cache"
    sys.modules["app.config"] = config_stub

    upload_references_stub = types.ModuleType("app.uploads.references")
    upload_references_stub.AnalysisInputError = ValueError
    upload_references_stub.resolve_analysis_inputs = lambda data, upload_root, lookup: [data.get("file_path")]
    sys.modules["app.uploads.references"] = upload_references_stub

    tasks_stub = types.ModuleType("app.analysis.routes.tasks")
    tasks_stub.analyze_log_task = FakeAnalyzeTask()
    sys.modules["app.analysis.routes.tasks"] = tasks_stub
    return flask_stub, tasks_stub, fake_celery, FakeOpenAI


def load_routes_module():
    flask_stub, tasks_stub, fake_celery, fake_openai = install_route_stubs()
    spec = importlib.util.spec_from_file_location("app.analysis.routes.routes", ROUTES_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["app.analysis.routes.routes"] = module
    spec.loader.exec_module(module)
    return module, flask_stub, tasks_stub, fake_celery, fake_openai


class AsyncAnalysisRouteTests(unittest.TestCase):
    def test_clone_git_repo_reports_sanitized_git_stderr(self):
        routes, flask_stub, _, _, _ = load_routes_module()
        flask_stub.current_app.config = {
            "GIT_USER": "tester",
            "GIT_PASSWORD": "top-secret",
        }
        original_run = routes.subprocess.run

        def fail_clone(*args, **kwargs):
            raise subprocess.CalledProcessError(
                128,
                args[0],
                stderr=b"fatal: Remote branch release-missing not found in upstream origin",
            )

        routes.subprocess.run = fail_clone
        try:
            with self.assertRaises(routes.GitCloneError) as raised:
                routes.clone_git_repo(
                    "https://git.example/team/repo.git",
                    "release-missing",
                    workspace_id="task-test",
                )
        finally:
            routes.subprocess.run = original_run

        message = str(raised.exception)
        self.assertIn("release-missing", message)
        self.assertIn("不存在", message)
        self.assertNotIn("top-secret", message)

    def test_primary_clone_failure_is_not_swallowed(self):
        routes, _, _, _, _ = load_routes_module()
        repositories = [{
            "role": "primary",
            "moduleName": "primary-repo",
            "branchAddress": "https://git.example/primary.git",
            "tagVersion": "missing",
            "workspaceId": "task",
        }]

        def fail_clone(*args, **kwargs):
            raise routes.GitCloneError("primary-repo 分支 missing 不存在")

        with self.assertRaises(routes.GitCloneError):
            routes.clone_analysis_repositories(repositories, fail_clone, max_workers=1)

    def test_validate_analysis_repositories_requires_exact_existing_tags(self):
        routes, _, _, _, _ = load_routes_module()
        repositories = [
            {
                "role": "primary",
                "moduleName": "see-task",
                "branchAddress": "https://git.example/see-task.git",
                "tagVersion": "v1.2.3",
            },
            {
                "role": "related",
                "moduleName": "bot-common",
                "branchAddress": "https://git.example/bot-common.git",
                "tagVersion": "missing-tag",
            },
        ]

        result = routes.validate_analysis_repositories(
            repositories,
            tag_checker=lambda _address, tag: {"exists": tag == "v1.2.3", "reason": ""},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["checked"], 2)
        self.assertEqual(len(result["missing"]), 1)
        self.assertEqual(result["missing"][0]["moduleName"], "bot-common")
        self.assertEqual(result["missing"][0]["tagVersion"], "missing-tag")

    def test_build_analysis_repositories_keeps_common_related_module(self):
        routes, _, _, _, _ = load_routes_module()
        repositories = routes.build_analysis_repositories({
            "moduleId": "1",
            "moduleName": "see-task",
            "branchAddress": "https://git.example/see-task.git",
            "tagVersion": "v1.2.3",
            "relatedModules": [{
                "moduleId": "2",
                "moduleName": "bot-common",
                "branchAddress": "https://git.example/bot-common.git",
                "common": True,
            }],
        }, "task-common")

        self.assertEqual([item["role"] for item in repositories], ["primary", "related"])
        self.assertEqual(repositories[1]["moduleName"], "bot-common")
        self.assertEqual(repositories[1]["tagVersion"], "")
        self.assertTrue(repositories[1]["metadataOnly"])
        self.assertEqual(repositories[1]["workspaceId"], "task-common")

    def test_common_reference_without_tag_does_not_require_remote_tag(self):
        routes, _, _, _, _ = load_routes_module()

        result = routes.validate_analysis_repositories(
            [{
                "role": "related",
                "moduleName": "bot-common",
                "branchAddress": "https://git.example/bot-common.git",
                "tagVersion": "",
                "common": True,
            }],
            tag_checker=lambda _address, _tag: self.fail("公共组件引用不应触发 Tag 校验"),
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["skippedCommon"][0]["moduleName"], "bot-common")

    def test_image_chain_discovery_uses_metadata_without_ai(self):
        routes, _, _, _, _ = load_routes_module()
        source = inspect.getsource(routes.build_chain_relevance_input)

        self.assertNotIn("analyze_uploaded_image", source)
        result = routes.build_chain_relevance_input({
            "source_type": "image",
            "file_paths": ["/tmp/callback-timeout.png", "/tmp/http-500.png"],
            "image_tag": "downstream callback",
            "image_description": "HTTP 500 response timeout",
        })

        self.assertIn("downstream callback", result)
        self.assertIn("HTTP 500 response timeout", result)
        self.assertIn("callback-timeout.png", result)

    def test_image_chain_context_uses_local_ocr_text_when_enabled(self):
        """显式开启本地 OCR 时，链路判断仍复用本地 OCR 文本。"""
        routes, _, _, _, _ = load_routes_module()

        def fake_ocr(paths):
            self.assertEqual(paths, ["/tmp/error.png"])
            return {
                "available": True,
                "engine": "paddleocr",
                "extracted_text": (
                    "2026-07-14 ERROR request failed\n"
                    "url=http://see-management-svc:9008/see-management/facade/prompt/get"
                ),
                "lines": [],
                "average_confidence": 0.96,
                "warnings": [],
            }

        with mock.patch.dict("os.environ", {"LOCAL_OCR_ENABLED": "true"}, clear=False):
            source_text, ocr_result = routes.build_chain_relevance_context(
                {
                    "source_type": "image",
                    "file_path": "/tmp/error.png",
                    "image_tag": "log_image",
                    "image_description": "调用失败截图",
                },
                ocr_extractor=fake_ocr,
            )

        self.assertIn("see-management-svc", source_text)
        self.assertIn("调用失败截图", source_text)
        self.assertEqual(ocr_result["engine"], "paddleocr")

    def test_image_chain_context_uses_model_by_default_without_local_ocr(self):
        """默认停用本地 OCR：链路判断直接使用模型识别结果，不调用本地 OCR。"""
        routes, _, _, _, _ = load_routes_module()

        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("LOCAL_OCR_ENABLED", None)
            original_ai = routes.analyze_uploaded_image
            routes.analyze_uploaded_image = lambda image_paths, image_tag, image_description="": {
                "summary": "模型识别摘要",
                "extracted_text": "url=http://see-management-svc:9008/see-management/facade/prompt/get",
                "keywords": ["error"],
                "components": ["see-management-svc"],
            }
            try:
                source_text, ocr_result = routes.build_chain_relevance_context(
                    {
                        "source_type": "image",
                        "file_path": "/tmp/error.png",
                        "image_tag": "log_image",
                        "image_description": "调用失败截图",
                    },
                    ocr_extractor=lambda paths: self.fail("本地 OCR 默认应停用"),
                )
            finally:
                routes.analyze_uploaded_image = original_ai

        self.assertIn("see-management-svc", source_text)
        self.assertIn("调用失败截图", source_text)
        self.assertEqual(ocr_result["engine"], "gpt-vision")
        self.assertTrue(ocr_result["available"])

    def test_image_chain_context_can_fall_back_to_gpt_when_local_ocr_is_disabled(self):
        routes, _, _, _, _ = load_routes_module()

        with mock.patch.dict("os.environ", {"LOCAL_OCR_ENABLED": "false"}, clear=False):
            original_ai = routes.analyze_uploaded_image
            routes.analyze_uploaded_image = lambda image_paths, image_tag, image_description="": {
                "summary": "gpt summary",
                "extracted_text": "GPT image text",
                "keywords": ["error"],
                "components": ["api"],
            }
            try:
                source_text, ocr_result = routes.build_chain_relevance_context(
                    {
                        "source_type": "image",
                        "file_path": "/tmp/error.png",
                        "image_tag": "log_image",
                        "image_description": "调用失败截图",
                    },
                    ocr_extractor=lambda paths: self.fail("local OCR should not be called"),
                )
            finally:
                routes.analyze_uploaded_image = original_ai

        self.assertIn("GPT image text", source_text)
        self.assertEqual(ocr_result["engine"], "gpt-vision")
        self.assertTrue(ocr_result["available"])

    def test_image_chain_context_can_fall_back_to_gpt_when_local_ocr_is_low_confidence(self):
        routes, _, _, _, _ = load_routes_module()

        original_ai = routes.analyze_uploaded_image
        routes.analyze_uploaded_image = lambda image_paths, image_tag, image_description="": {
            "summary": "gpt summary",
            "extracted_text": "GPT image text",
            "keywords": ["error"],
            "components": ["api"],
        }
        try:
            source_text, ocr_result = routes.build_chain_relevance_context(
                {
                    "source_type": "image",
                    "file_path": "/tmp/error.png",
                    "image_tag": "log_image",
                    "image_description": "调用失败截图",
                },
                ocr_extractor=lambda paths: {
                    "available": True,
                    "engine": "paddleocr",
                    "extracted_text": "low confidence text",
                    "lines": [],
                    "average_confidence": 0.1,
                    "warnings": [],
                },
            )
        finally:
            routes.analyze_uploaded_image = original_ai

        self.assertIn("GPT image text", source_text)
        self.assertEqual(ocr_result["engine"], "gpt-vision")
        self.assertTrue(ocr_result["available"])

    def test_discovery_response_returns_image_ocr_for_task_reuse(self):
        routes, flask_stub, _, _, _ = load_routes_module()
        flask_stub.request.json = {
            "productId": "46",
            "moduleId": "256",
            "branchAddress": "https://git.example/see-task.git",
            "tagVersion": "v1",
            "file_path": "/tmp/error.png",
            "source_type": "image",
            "image_tag": "log_image",
        }
        expected_ocr = {
            "available": True,
            "engine": "paddleocr",
            "extracted_text": "ERROR callback http://see-management-svc/api failed",
            "lines": [],
            "average_confidence": 0.95,
            "warnings": [],
        }
        original_context_builder = routes.build_chain_relevance_context
        original_modules_loader = routes.get_available_modules_for_product
        routes.build_chain_relevance_context = lambda data: (
            expected_ocr["extracted_text"],
            expected_ocr,
        )
        routes.get_available_modules_for_product = lambda product_id: []
        try:
            payload = routes.discover_related_modules_route()
        finally:
            routes.build_chain_relevance_context = original_context_builder
            routes.get_available_modules_for_product = original_modules_loader

        self.assertTrue(payload["requiresRelatedEvidence"])
        self.assertEqual(payload["imageOcr"], expected_ocr)

    def test_image_discovery_reports_ocr_unavailable_instead_of_no_chain(self):
        routes, flask_stub, _, _, _ = load_routes_module()
        flask_stub.request.json = {
            "productId": "46",
            "moduleId": "256",
            "branchAddress": "https://git.example/see-task.git",
            "tagVersion": "v1",
            "file_path": "/tmp/error.png",
            "source_type": "image",
            "image_tag": "log_image",
        }
        unavailable_ocr = {
            "available": False,
            "engine": "paddleocr",
            "extracted_text": "",
            "lines": [],
            "average_confidence": 0.0,
            "warnings": ["No module named 'paddleocr'"],
        }
        original_context_builder = routes.build_chain_relevance_context
        routes.build_chain_relevance_context = lambda data: ("log_image", unavailable_ocr)
        try:
            payload = routes.discover_related_modules_route()
        finally:
            routes.build_chain_relevance_context = original_context_builder

        self.assertEqual(payload["status"], "image_ocr_unavailable")
        self.assertFalse(payload["chainAssessmentComplete"])
        self.assertNotIn("不涉及上下游", payload["message"])
        self.assertEqual(payload["imageOcr"], unavailable_ocr)

    def test_sync_analysis_uses_backend_summary_instead_of_preliminary_ai(self):
        routes, _, _, _, _ = load_routes_module()
        source = inspect.getsource(routes.analyze_log_and_code)

        self.assertNotIn("analyze_log_with_deepseek", source)
        self.assertIn("build_backend_log_analysis", source)

    def test_final_image_analysis_uses_one_multimodal_request(self):
        routes, flask_stub, _, _, fake_openai = load_routes_module()
        flask_stub.current_app.config = {
            "OPENAI_KEY": "token",
            "OPENAI_URL": "https://example.invalid/v1",
            "OPENAI_MODEL": "gpt-test",
            "OPENAI_API_STYLE": "chat",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "error.png"
            image_path.write_bytes(b"fake-png")

            result = routes.analyze_code_with_deepseek(
                "image_tag=log_image",
                "backend image metadata",
                [],
                image_paths=[str(image_path)],
            )

        self.assertEqual(result, "chat-result")
        self.assertEqual(len(fake_openai.last_instance.chat.completions.calls), 1)
        content = fake_openai.last_instance.chat.completions.calls[0]["messages"][1]["content"]
        self.assertTrue(any(item.get("type") == "image_url" for item in content))

    def test_final_text_prompt_includes_grouped_backend_summary_once(self):
        routes, flask_stub, _, _, fake_openai = load_routes_module()
        flask_stub.current_app.config = {
            "OPENAI_KEY": "token",
            "OPENAI_URL": "https://example.invalid/v1",
            "OPENAI_MODEL": "gpt-test",
            "OPENAI_API_STYLE": "chat",
        }
        log_content = """2026-06-24 10:00:00 ERROR complaint failed
java.lang.NullPointerException: null
    at demo.Complaint.run(Complaint.java:42)
"""
        backend_summary = routes.build_backend_log_analysis(log_content)

        routes.analyze_code_with_deepseek(log_content, backend_summary, [])

        prompt = fake_openai.last_instance.chat.completions.calls[0]["messages"][1]["content"]
        self.assertEqual(prompt.count("grouped_issue_count:"), 1)

    def test_routes_source_avoids_python310_fstring_backslash_expressions(self):
        source = ROUTES_PATH.read_text(encoding="utf-8")
        offenders = []
        f_string_prefix_pattern = re.compile(r"(^|[\s\(\[=,:])(?:[rRbBuU]*[fF][rRbBuU]*|[fF][rRbBuU]*)['\"]")
        expression_pattern = re.compile(r"\{[^{}]*\\[^{}]*\}")

        for line_number, line in enumerate(source.splitlines(), start=1):
            if "\\" not in line or not f_string_prefix_pattern.search(line):
                continue
            if expression_pattern.search(line):
                offenders.append(f"{line_number}: {line.strip()}")

        self.assertEqual(
            offenders,
            [],
            "Python 3.10 cannot parse backslashes inside f-string expressions.",
        )

    def test_submit_async_returns_task_id(self):
        routes, flask_stub, tasks_stub, _, _ = load_routes_module()
        flask_stub.request.json = {
            "productId": "1",
            "moduleId": "2",
            "branchAddress": "https://git.example/repo.git",
            "tagVersion": "v1",
            "file_path": "/data/upload/log.txt",
        }

        payload, status = routes.submit_async_analysis()

        self.assertEqual(status, 202)
        self.assertEqual(payload["task_id"], "task-123")
        self.assertEqual(tasks_stub.analyze_log_task.submitted[0]["productId"], "1")

    def test_submit_async_validates_required_fields(self):
        routes, flask_stub, _, _, _ = load_routes_module()
        flask_stub.request.json = {"productId": "1"}

        payload, status = routes.submit_async_analysis()

        self.assertEqual(status, 400)
        self.assertIn("moduleId", payload["missing_fields"])

    def test_submit_async_requires_image_tag_for_image_source(self):
        routes, flask_stub, _, _, _ = load_routes_module()
        flask_stub.request.json = {
            "productId": "1",
            "moduleId": "2",
            "branchAddress": "https://git.example/repo.git",
            "tagVersion": "v1",
            "file_path": "/data/upload/demo-image.png",
            "source_type": "image",
        }

        payload, status = routes.submit_async_analysis()

        self.assertEqual(status, 400)
        self.assertIn("image_tag", payload["missing_fields"])

    def test_get_task_status_returns_success_result(self):
        routes, _, _, _, _ = load_routes_module()

        payload = routes.get_analysis_task("task-123")

        self.assertTrue(payload["ready"])
        self.assertTrue(payload["successful"])
        self.assertEqual(payload["result"]["code_analysis"], "done")

    def test_get_task_status_returns_progress_meta(self):
        original_info = FakeAsyncResult.info
        FakeAsyncResult.info = {
            "stage": "clone_repo_done",
            "stage_label": "浠ｇ爜鎷夊彇",
            "stage_percent": 100,
            "percent": 45,
            "message": "浠ｇ爜鎷夊彇 100%",
        }
        try:
            routes, _, _, _, _ = load_routes_module()

            payload = routes.get_analysis_task("task-123")
        finally:
            FakeAsyncResult.info = original_info

        self.assertEqual(payload["progress"]["stage"], "clone_repo_done")
        self.assertEqual(payload["progress"]["message"], "浠ｇ爜鎷夊彇 100%")
        self.assertEqual(payload["progress"]["percent"], 45)

    def test_cancel_task_revokes_celery_task(self):
        routes, _, _, fake_celery, _ = load_routes_module()

        payload, status = routes.cancel_analysis_task("task-123")

        self.assertEqual(status, 202)
        self.assertEqual(payload["state"], "REVOKED")
        self.assertEqual(fake_celery.control.revoked[0]["task_id"], "task-123")
        self.assertTrue(fake_celery.control.revoked[0]["terminate"])
        self.assertEqual(fake_celery.control.revoked[0]["signal"], "SIGTERM")

    def test_insert_query_record_accepts_business_status_keyword(self):
        routes, flask_stub, _, _, _ = load_routes_module()
        created_records = []

        def fake_query_record(**kwargs):
            created_records.append(kwargs)
            return kwargs

        routes.QueryRecord = fake_query_record
        routes.db = types.SimpleNamespace(session=types.SimpleNamespace(
            add=lambda *args, **kwargs: None,
            commit=lambda *args, **kwargs: None,
            rollback=lambda *args, **kwargs: None,
        ))
        flask_stub.current_app.config = {"OPENAI_MODEL": "gpt-5.5"}

        routes.insert_query_record(
            {
                "productId": "45",
                "moduleId": "251",
                "log_id": 25,
                "file_path": "/data/upload/demo.log",
                "branchAddress": "https://code.in.wezhuiyi.com/pal/demo.git",
                "tagVersion": "v1.0.0",
            },
            0,
            status="cache_hit",
            hit_cache=True,
            knowledge_case_id=7,
        )

        self.assertEqual(created_records[0]["answer"], 0)
        self.assertEqual(created_records[0]["status"], "cache_hit")
        self.assertTrue(created_records[0]["hit_cache"])
        self.assertEqual(created_records[0]["knowledge_case_id"], 7)

    def test_ai_model_uses_chat_completions_by_default(self):
        routes, flask_stub, _, _, fake_openai = load_routes_module()
        flask_stub.current_app.config = {
            "OPENAI_KEY": "token",
            "OPENAI_URL": "https://newapi.in.wezhuiyi.com/v1",
            "OPENAI_MODEL": "gpt-5-codex",
        }

        result = routes.call_ai_model("system prompt", "user prompt")

        self.assertEqual(result, "chat-result")
        self.assertEqual(fake_openai.last_instance.base_url, "https://newapi.in.wezhuiyi.com/v1")
        call = fake_openai.last_instance.chat.completions.calls[0]
        self.assertEqual(call["model"], "gpt-5-codex")
        self.assertEqual(call["messages"][0]["role"], "system")

    def test_chat_ai_model_sends_high_reasoning_effort(self):
        routes, flask_stub, _, _, fake_openai = load_routes_module()
        flask_stub.current_app.config = {
            "OPENAI_KEY": "token",
            "OPENAI_URL": "https://newapi.in.wezhuiyi.com/v1",
            "OPENAI_MODEL": "gpt-5.6-sol",
            "OPENAI_API_STYLE": "chat",
            "OPENAI_REASONING_EFFORT": "high",
        }

        routes.call_ai_model("system prompt", "user prompt")

        call = fake_openai.last_instance.chat.completions.calls[0]
        self.assertEqual(call["model"], "gpt-5.6-sol")
        self.assertEqual(call["reasoning_effort"], "high")

    def test_ai_model_can_use_responses_api_style(self):
        routes, flask_stub, _, _, fake_openai = load_routes_module()
        flask_stub.current_app.config = {
            "OPENAI_KEY": "token",
            "OPENAI_URL": "https://newapi.in.wezhuiyi.com/v1",
            "OPENAI_MODEL": "gpt-5-codex",
            "OPENAI_API_STYLE": "responses",
        }

        result = routes.call_ai_model("system prompt", "user prompt")

        self.assertEqual(result, "responses-result")
        call = fake_openai.last_instance.responses.calls[0]
        self.assertEqual(call["model"], "gpt-5-codex")
        self.assertEqual(call["input"][0]["role"], "system")

    def test_responses_ai_model_sends_high_reasoning_effort(self):
        routes, flask_stub, _, _, fake_openai = load_routes_module()
        flask_stub.current_app.config = {
            "OPENAI_KEY": "token",
            "OPENAI_URL": "https://newapi.in.wezhuiyi.com/v1",
            "OPENAI_MODEL": "gpt-5.6-sol",
            "OPENAI_API_STYLE": "responses",
            "OPENAI_REASONING_EFFORT": "high",
        }

        routes.call_ai_model("system prompt", "user prompt")

        call = fake_openai.last_instance.responses.calls[0]
        self.assertEqual(call["model"], "gpt-5.6-sol")
        self.assertEqual(call["reasoning"], {"effort": "high"})

    def test_multimodal_ai_model_uses_image_input_blocks(self):
        routes, flask_stub, _, _, fake_openai = load_routes_module()
        flask_stub.current_app.config = {
            "OPENAI_KEY": "token",
            "OPENAI_URL": "https://newapi.in.wezhuiyi.com/v1",
            "OPENAI_MODEL": "gpt-5.5",
            "OPENAI_API_STYLE": "responses",
        }

        result = routes.call_ai_multimodal_model(
            "system prompt",
            "user prompt",
            "data:image/png;base64,ZmFrZQ==",
        )

        self.assertEqual(result, "responses-result")
        call = fake_openai.last_instance.responses.calls[0]
        self.assertEqual(call["model"], "gpt-5.5")
        self.assertEqual(call["input"][1]["content"][1]["type"], "input_image")

    def test_multimodal_ai_model_accepts_multiple_image_inputs(self):
        routes, flask_stub, _, _, fake_openai = load_routes_module()
        flask_stub.current_app.config = {
            "OPENAI_KEY": "token",
            "OPENAI_URL": "https://newapi.in.wezhuiyi.com/v1",
            "OPENAI_MODEL": "gpt-5.5",
            "OPENAI_API_STYLE": "responses",
        }

        result = routes.call_ai_multimodal_model(
            "system prompt",
            "user prompt",
            [
                "data:image/png;base64,ZmFrZTE=",
                "data:image/png;base64,ZmFrZTI=",
            ],
        )

        self.assertEqual(result, "responses-result")
        content = fake_openai.last_instance.responses.calls[0]["input"][1]["content"]
        self.assertEqual([item["type"] for item in content], ["input_text", "input_image", "input_image"])

    def test_multi_image_recognition_prompt_requires_per_image_findings(self):
        routes, flask_stub, _, _, fake_openai = load_routes_module()
        flask_stub.current_app.config = {
            "OPENAI_KEY": "token",
            "OPENAI_URL": "https://example.invalid/v1",
            "OPENAI_MODEL": "gpt-test",
            "OPENAI_API_STYLE": "chat",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            image_paths = []
            for name in ("first.png", "second.png"):
                image_path = Path(temp_dir) / name
                image_path.write_bytes(b"fake-png")
                image_paths.append(str(image_path))
            routes.analyze_uploaded_image(image_paths, "business_image", "两张错误截图")

        content = fake_openai.last_instance.chat.completions.calls[0]["messages"][1]["content"]
        prompt = next(item["text"] for item in content if item.get("type") == "text")
        self.assertIn("逐张输出识别结果", prompt)
        self.assertIn("第1张、第2张", prompt)
        self.assertIn("只有确认是同一错误的重复截图时才允许合并", prompt)

    def test_build_image_analysis_context_includes_description_keywords_and_text(self):
        routes, _, _, _, _ = load_routes_module()

        context = routes.build_image_analysis_context(
            {
                "image_tag": "business_image",
                "summary": "订单详情页提示 Redis 连接异常",
                "extracted_text": "Redis connection reset by peer",
                "keywords": ["redis", "order", "timeout"],
                "components": ["redis"],
            },
            "点击保存订单时报错",
        )

        self.assertIn("业务截图", context)
        self.assertIn("点击保存订单时报错", context)
        self.assertIn("Redis connection reset by peer", context)
        self.assertIn("redis", context.lower())

    def test_code_analysis_prompt_includes_raw_log_summary_and_code(self):
        routes, flask_stub, _, _, fake_openai = load_routes_module()
        flask_stub.current_app.config = {
            "OPENAI_KEY": "token",
            "OPENAI_URL": "https://newapi.in.wezhuiyi.com/v1",
            "OPENAI_MODEL": "gpt-5.5",
        }
        code_snippets = [{
            "file": "src/main/java/Demo.java",
            "line": 42,
            "snippet": "client.newCall(request).execute();",
        }]

        result = routes.analyze_code_with_deepseek(
            "A connection was leaked.",
            "Initial analysis says OkHttp ResponseBody was not closed.",
            code_snippets,
        )

        self.assertEqual(result, "chat-result")
        prompt = fake_openai.last_instance.chat.completions.calls[0]["messages"][1]["content"]
        self.assertIn("A connection was leaked.", prompt)
        self.assertIn("Initial analysis says OkHttp ResponseBody was not closed.", prompt)
        self.assertIn("client.newCall(request).execute();", prompt)
        self.assertIn("src/main/java/Demo.java", prompt)
        self.assertIn("src/main/java/Demo.java", prompt)
        self.assertIn("client.newCall(request).execute()", prompt)
        self.assertIn("完整日志上下文", prompt)
        self.assertIn("possible_causes", prompt)
        self.assertIn("config_issue", prompt)
        self.assertIn("network_issue", prompt)
        self.assertIn("code_issue", prompt)
        self.assertIn("query_commands", prompt)
        self.assertIn("fix_commands", prompt)

    def test_code_analysis_prompt_includes_common_component_reference_rule(self):
        routes, flask_stub, _, _, fake_openai = load_routes_module()
        flask_stub.current_app.config = {
            "OPENAI_KEY": "token",
            "OPENAI_URL": "https://newapi.in.wezhuiyi.com/v1",
            "OPENAI_MODEL": "gpt-5.5",
        }

        routes.analyze_code_with_deepseek(
            "failed to load effect config",
            "backend summary",
            [],
            common_repositories=[{
                "moduleName": "see-common",
                "branchAddress": "https://code.in.wezhuiyi.com/see/see-common.git",
                "common": True,
            }],
        )

        prompt = fake_openai.last_instance.chat.completions.calls[0]["messages"][1]["content"]
        self.assertIn("公共组件引用", prompt)
        self.assertIn("see-common", prompt)
        self.assertIn("本缺陷引用了 xxx 公共组件功能，关键代码在 <file>:<line>", prompt)
        self.assertIn("不得编造", prompt)

    def test_log_analysis_prompt_requires_full_context_not_only_error_lines(self):
        routes, flask_stub, _, _, fake_openai = load_routes_module()
        flask_stub.current_app.config = {
            "OPENAI_KEY": "token",
            "OPENAI_URL": "https://newapi.in.wezhuiyi.com/v1",
            "OPENAI_MODEL": "gpt-5.5",
        }
        log_content = "\n".join([
            "2026-06-07 12:30:44 INFO request started",
            "2026-06-07 12:30:45 WARN redis latency is high",
            "2026-06-07 12:30:46 ERROR failed",
            "2026-06-07 12:30:47 INFO request finished",
        ])

        result = routes.analyze_log_with_deepseek(log_content)

        self.assertEqual(result, "chat-result")
        prompt = fake_openai.last_instance.chat.completions.calls[0]["messages"][1]["content"]
        self.assertIn("完整日志上下文", prompt)
        self.assertIn("不要只根据 ERROR", prompt)
        self.assertIn("INFO request started", prompt)
        self.assertIn("WARN redis latency is high", prompt)
        self.assertIn("配置", prompt)
        self.assertIn("网络", prompt)

    def test_large_log_analysis_uses_error_context_instead_of_chunking_full_log(self):
        routes, _, _, _, _ = load_routes_module()
        original_call_ai_model = routes.call_ai_model
        calls = []

        def fake_call_ai_model(system_prompt, user_prompt):
            calls.append(user_prompt)
            return f"summary-{len(calls)}"

        routes.call_ai_model = fake_call_ai_model
        try:
            log_content = "\n".join(
                [f"2026-06-07 12:29:{index:02d} INFO noise {index}" for index in range(20)]
                + [
                    "2026-06-07 12:30:44 INFO request started",
                    "2026-06-07 12:30:45 WARN redis latency is high",
                    "2026-06-07 12:30:46 ERROR failed",
                    "    at demo.Target.run(Target.java:42)",
                    "2026-06-07 12:30:47 INFO request finished",
                ]
                + [f"2026-06-07 12:31:{index:02d} INFO tail noise {index}" for index in range(20)]
            )

            result = routes.analyze_log_with_deepseek(log_content)
        finally:
            routes.call_ai_model = original_call_ai_model

        self.assertEqual(result, "summary-1")
        self.assertEqual(len(calls), 1)
        self.assertIn("\u65e5\u5fd7\u9519\u8bef\u5173\u952e\u6bb5\u53ca\u4e0a\u4e0b\u6587", calls[0])
        self.assertIn("INFO request started", calls[0])
        self.assertIn("WARN redis latency is high", calls[0])
        self.assertIn("ERROR failed", calls[0])
        self.assertIn("Target.java:42", calls[0])
        self.assertNotIn("INFO noise 0", calls[0])
        self.assertNotIn("tail noise 19", calls[0])

    def test_full_log_context_preserves_non_contiguous_error_events(self):
        routes, _, _, _, _ = load_routes_module()
        log_content = "\n".join([
            "2026-06-24 18:01:37.332 INFO url=http://see-management-svc:9008/see-management/facade/llm-prompt/getSystemPrompt?typeCode=-2",
            "2026-06-24 18:01:37.348 ERROR 40 --- ComplaintAnalysisService : complaint analysis error",
            "java.lang.NullPointerException: null",
            "    at com.zhuiyi.see.task.service.impl.ComplaintAnalysisService.doAnalyze(ComplaintAnalysisService.java:143)",
            "2026-06-24 18:02:10.001 INFO unrelated heartbeat",
            "2026-06-24 18:03:11.109 ERROR 40 --- GraphClientInterceptor : graph rpc unavailable",
            "message: \"OnceQuerier doRequest return err:rpc error: code = Unavailable desc = connection closed\"",
            "2026-06-24 18:04:00.001 INFO tail heartbeat",
        ])

        context = routes.build_full_log_analysis_context(log_content)

        self.assertIn("grouped_issue_count: 2", context)
        self.assertIn("[issue_group_1]", context)
        self.assertIn("first_line: 2", context)
        self.assertIn("ComplaintAnalysisService.java:143", context)
        self.assertIn("[issue_group_2]", context)
        self.assertIn("first_line: 6", context)
        self.assertIn("graph rpc unavailable", context)
        self.assertIn("connection closed", context)

    def test_code_analysis_compacts_oversized_log_context(self):
        routes, flask_stub, _, _, fake_openai = load_routes_module()
        flask_stub.current_app.config = {
            "OPENAI_KEY": "token",
            "OPENAI_URL": "https://newapi.in.wezhuiyi.com/v1",
            "OPENAI_MODEL": "gpt-5.5",
            "LOG_CONTEXT_MAX_CHARS": 2000,
        }
        large_log = "\n".join([
            "2026-06-07 12:30:44 INFO request started",
            "2026-06-07 12:30:45 WARN redis latency is high",
            "2026-06-07 12:30:46 ERROR failed with important stack",
            "2026-06-07 12:30:47 INFO request finished",
        ])

        result = routes.analyze_code_with_deepseek(
            large_log,
            "error context summary",
            [{"file": "Demo.java", "line": 42, "snippet": "throw ex;"}],
        )

        self.assertEqual(result, "chat-result")
        prompt = fake_openai.last_instance.chat.completions.calls[0]["messages"][1]["content"]
        self.assertIn("\u65e5\u5fd7\u9519\u8bef\u5173\u952e\u6bb5\u53ca\u4e0a\u4e0b\u6587", prompt)
        self.assertIn("ERROR failed with important stack", prompt)
        self.assertNotIn(large_log, prompt)

    def test_extracts_python_traceback_file_and_line(self):
        routes, _, _, _, _ = load_routes_module()
        log_content = '''Traceback (most recent call last):
  File "app.py", line 42, in <module>
    result = process_data(data)
  File "utils.py", line 18, in process_data
    return data / 0
ZeroDivisionError: division by zero
'''

        result = routes.extract_error_info_from_log(log_content)

        self.assertEqual(result[0]["file"], "app.py")
        self.assertEqual(result[0]["line"], 42)
        self.assertEqual(result[0]["error"], "ZeroDivisionError: division by zero")
        self.assertEqual(result[1]["file"], "utils.py")
        self.assertEqual(result[1]["line"], 18)

    def test_resolves_python_traceback_and_extracts_code_snippet(self):
        routes, _, _, _, _ = load_routes_module()
        with tempfile.TemporaryDirectory() as repo_dir:
            app_file = Path(repo_dir) / "app.py"
            app_file.write_text(
                "\n".join(f"line {index}" for index in range(1, 51)),
                encoding="utf-8",
            )

            resolved = routes.resolve_file_paths(
                repo_dir,
                [{"file": "app.py", "line": 42, "error": "ZeroDivisionError: division by zero"}],
            )
            snippets = routes.extract_code_snippets(resolved)

        self.assertEqual(len(snippets), 1)
        self.assertEqual(snippets[0]["line"], 42)
        self.assertIn("app.py", snippets[0]["file"])
        self.assertIn("line 42", snippets[0]["snippet"])
        self.assertEqual(snippets[0]["start_line"], 37)
        self.assertEqual(snippets[0]["end_line"], 46)
        self.assertIn(">>   42 | line 42", snippets[0]["numbered_snippet"])

    def test_extracts_go_panic_file_and_line(self):
        routes, _, _, _, _ = load_routes_module()
        log_content = '''panic: runtime error: invalid memory address or nil pointer dereference

goroutine 42 [running]:
algorithm-platform/internal/trainer.(*Runner).Train(0xc00010a000)
        /workspace/algorithm-platform/internal/trainer/runner.go:42 +0x12f
main.main()
        /workspace/algorithm-platform/cmd/train/main.go:18 +0x45
'''

        result = routes.extract_error_info_from_log(log_content)

        self.assertEqual(result[0]["file"], "runner.go")
        self.assertEqual(result[0]["line"], 42)
        self.assertEqual(result[0]["error"], "panic: runtime error: invalid memory address or nil pointer dereference")
        self.assertEqual(result[1]["file"], "main.go")
        self.assertEqual(result[1]["line"], 18)

    def test_extracts_shell_script_file_and_line(self):
        routes, _, _, _, _ = load_routes_module()
        log_content = """deploy.sh: line 27: kubectl: command not found
worker.sh:43: exit status 1
"""

        result = routes.extract_error_info_from_log(log_content)

        self.assertEqual(
            [(item["file"], item["line"]) for item in result],
            [("deploy.sh", 27), ("worker.sh", 43)],
        )
        self.assertTrue(all(item["language"] == "shell" for item in result))

    def test_go_files_are_scanned_for_component_usages(self):
        routes, _, _, _, _ = load_routes_module()
        with tempfile.TemporaryDirectory() as repo_dir:
            go_file = Path(repo_dir) / "cache.go"
            go_file.write_text(
                "\n".join([
                    "package trainer",
                    "",
                    "import \"github.com/redis/go-redis/v9\"",
                    "",
                    "func cacheClient() *redis.Client {",
                    "    return redis.NewClient(&redis.Options{})",
                    "}",
                ]),
                encoding="utf-8",
            )

            components = routes.detect_error_components("redis dial tcp timeout")
            snippets = routes.find_component_code_usages(repo_dir, components)

        self.assertIn("redis", components)
        self.assertTrue(any("cache.go" in item["file"] for item in snippets))
        self.assertTrue(any("go-redis" in item["numbered_snippet"] for item in snippets))

    def test_supported_source_suffixes_include_java_python_go_and_shell(self):
        routes, _, _, _, _ = load_routes_module()

        self.assertIn(".java", routes.SOURCE_CODE_SUFFIXES)
        self.assertIn(".py", routes.SOURCE_CODE_SUFFIXES)
        self.assertIn(".go", routes.SOURCE_CODE_SUFFIXES)
        self.assertTrue({".sh", ".bash", ".zsh"}.issubset(routes.SOURCE_CODE_SUFFIXES))

    def test_assesses_chain_relevance_when_downstream_response_fails(self):
        routes, _, _, _, _ = load_routes_module()

        result = routes.assess_chain_relevance(
            "ERROR call downstream service failed, http status=500, response error"
        )

        self.assertTrue(result["requiresRelatedEvidence"])
        self.assertEqual(result["status"], "need_related_evidence")
        self.assertIn("上下游", result["message"])

    def test_normalizes_openai_usage_limit_error(self):
        routes, _, _, _, _ = load_routes_module()
        raw_error = Exception("Error code: 429 - usage_limit_reached")
        raw_error.status_code = 429
        raw_error.response = FakeAiResponse()

        normalized = routes.normalize_ai_exception(raw_error)

        self.assertIsInstance(normalized, routes.AiServiceError)
        self.assertEqual(normalized.status_code, 429)
        self.assertEqual(normalized.error_type, "usage_limit_reached")
        self.assertIn("额度", str(normalized))

    def test_assesses_no_chain_relevance_for_local_exception(self):
        routes, _, _, _, _ = load_routes_module()

        result = routes.assess_chain_relevance(
            "java.lang.NullPointerException: cannot invoke method"
        )

        self.assertFalse(result["requiresRelatedEvidence"])
        self.assertEqual(result["status"], "no_related_evidence")
        self.assertEqual(result["message"], "此问题不涉及上下游链路判断，开始分析故障原因。")

    def test_discovers_related_modules_by_name_from_log_and_primary_code(self):
        routes, _, _, _, _ = load_routes_module()
        with tempfile.TemporaryDirectory() as repo_dir:
            source_file = Path(repo_dir) / "TrainerClient.java"
            source_file.write_text(
                "\n".join([
                    "class TrainerClient {",
                    "  String publish = \"dialogos publish training task\";",
                    "  String callback = \"java-callback-module/result\";",
                    "}",
                ]),
                encoding="utf-8",
            )
            available_modules = [
                {"module_id": 1, "module_name": "algorithm-platform"},
                {"module_id": 2, "module_name": "dialogos"},
                {"module_id": 3, "module_name": "java-callback-module"},
            ]

            related = routes.discover_related_modules(
                repo_dir,
                "algorithm-platform training failed, callback waits for java-callback-module",
                available_modules,
                primary_module_id=1,
            )

        by_name = {item["moduleName"]: item for item in related}
        self.assertEqual(set(by_name), {"dialogos", "java-callback-module"})
        self.assertEqual(by_name["dialogos"]["moduleId"], "2")
        self.assertIn("主模块代码", by_name["dialogos"]["reason"])
        self.assertIn("日志", by_name["java-callback-module"]["reason"])

    def test_discovers_related_module_from_log_text_and_infers_downstream(self):
        routes, _, _, _, _ = load_routes_module()

        related = routes.discover_related_modules_from_text(
            "ERROR SessionAsrTransferService asr transform error, http status=500",
            [
                {"module_id": 1, "module_name": "manifest"},
                {"module_id": 2, "module_name": "asr"},
            ],
            primary_module_id=1,
        )

        self.assertEqual(len(related), 1)
        self.assertEqual(related[0]["moduleName"], "asr")
        self.assertEqual(related[0]["role"], "downstream")

    def test_infers_missing_module_from_log_when_repo_list_has_no_match(self):
        routes, _, _, _, _ = load_routes_module()

        related = routes.discover_related_modules_from_text(
            "com.zhuiyi.see.task.exception.SeeTaskException: 调用离线asr转写语音失败",
            [{"module_id": 1, "module_name": "manifest"}],
            primary_module_id=1,
        )

        names = {item["moduleName"] for item in related}
        self.assertIn("see-task", names)
        self.assertTrue(all(not item["moduleId"] for item in related))
        self.assertIn("仓库", related[0]["reason"])

    def test_does_not_infer_module_from_plain_camel_class_name(self):
        routes, _, _, _, _ = load_routes_module()

        related = routes.discover_related_modules_from_text(
            'message: "OnceQuerier doRequest return err:rpc error: code = Unavailable"',
            [{"module_id": 1, "module_name": "manifest"}],
            primary_module_id=1,
        )

        self.assertEqual(related, [])

    def test_infers_missing_module_from_http_service_url(self):
        routes, _, _, _, _ = load_routes_module()

        related = routes.discover_related_modules_from_text(
            "url=http://see-management-svc:9008/see-management/facade/llm-prompt/getSystemPrompt?typeCode=-2, queryString=typeCode=-2",
            [{"module_id": 1, "module_name": "manifest"}],
            primary_module_id=1,
        )

        self.assertEqual([item["moduleName"] for item in related], ["see-management"])

    def test_ignores_storage_url_bucket_when_package_names_real_module(self):
        routes, _, _, _, _ = load_routes_module()

        related = routes.discover_related_modules_from_text(
            "com.zhuiyi.see.task.exception.SeeTaskException: Can not download file, please check url: "
            "http://minio-cluster:9000/zhuiyi-see/mock/dataset-schedule/audio/mock-audio-18.wav",
            [{"module_id": 1, "module_name": "manifest"}],
            primary_module_id=1,
        )

        self.assertEqual([item["moduleName"] for item in related], ["see-task"])

    def test_splits_chain_issue_candidates_with_previous_http_trace_context(self):
        routes, _, _, _, _ = load_routes_module()

        candidates = routes.split_chain_issue_candidates(
            "\n".join([
                "2026-06-24 18:01:37.332 INFO PromptServiceImpl 获取系统提示: -2",
                "2026-06-24 18:01:37.333 INFO HttpTracedInterceptor url=http://see-management-svc:9008/see-management/facade/llm-prompt/getSystemPrompt?typeCode=-2, queryString=typeCode=-2",
                "2026-06-24 18:01:37.347 INFO HttpTracedInterceptor call use cost:14ms",
                "2026-06-24 18:01:37.348 ERROR ComplaintAnalysisService complaint analysis error",
                "java.lang.NullPointerException: null",
                "    at com.zhuiyi.see.task.service.impl.ComplaintAnalysisService.doAnalyze(ComplaintAnalysisService.java:143)",
            ]),
            [
                {"module_id": 1, "module_name": "manifest"},
                {"module_id": 2, "module_name": "see-management"},
            ],
            primary_module_id=1,
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["relatedModules"][0]["moduleName"], "see-management")
        self.assertIn("see-management-svc", candidates[0]["issueSummary"])

    def test_splits_chain_issue_candidates_scans_more_than_twelve_error_blocks(self):
        routes, _, _, _, _ = load_routes_module()
        lines = []
        modules = [{"module_id": 1, "module_name": "manifest"}]
        for index in range(15):
            module_name = f"remote-{index}"
            modules.append({"module_id": index + 2, "module_name": module_name})
            lines.extend([
                f"2026-06-24 18:00:{index:02d}.001 INFO HttpTracedInterceptor url=http://{module_name}-svc:9008/{module_name}/api",
                f"2026-06-24 18:00:{index:02d}.002 ERROR RemoteCallService remote-{index} call failed",
                f"java.lang.RuntimeException: remote-{index} failed",
                f"2026-06-24 18:00:{index:02d}.003 INFO finished block {index}",
            ])

        candidates = routes.split_chain_issue_candidates(
            "\n".join(lines),
            modules,
            primary_module_id=1,
        )

        self.assertEqual(len(candidates), 15)
        self.assertEqual(candidates[-1]["relatedModules"][0]["moduleName"], "remote-14")

    def test_relevant_log_context_keeps_last_error_batch_when_truncated(self):
        routes, _, _, _, _ = load_routes_module()
        lines = []
        for index in range(25):
            lines.extend([
                f"2026-06-24 18:00:{index:02d}.001 INFO prepare block {index}",
                f"2026-06-24 18:00:{index:02d}.002 ERROR early service-{index} failed with timeout",
                f"java.lang.RuntimeException: early-{index}",
            ])
        lines.extend([
            "2026-06-24 19:59:58.001 INFO HttpTracedInterceptor url=http://see-management-svc:9008/see-management/api",
            "2026-06-24 19:59:59.002 ERROR final batch complaint analysis error",
            "java.lang.NullPointerException: final-marker",
        ])

        context = routes.extract_relevant_log_context(
            "\n".join(lines),
            context_lines=1,
            max_chars=1800,
        )

        self.assertIn("early service-0 failed", context)
        self.assertIn("final batch complaint analysis error", context)
        self.assertIn("see-management-svc", context)

    def test_splits_chain_issue_candidates_by_distinct_modules(self):
        routes, _, _, _, _ = load_routes_module()

        candidates = routes.split_chain_issue_candidates(
            "\n".join([
                "2026-06-24 ERROR SessionAsrTransferService asr transform error http status=500",
                "2026-06-24 ERROR DialogosCallbackClient dialogos callback timeout",
            ]),
            [
                {"module_id": 1, "module_name": "manifest"},
                {"module_id": 2, "module_name": "asr"},
                {"module_id": 3, "module_name": "dialogos"},
            ],
            primary_module_id=1,
        )

        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]["relatedModules"][0]["moduleName"], "asr")
        self.assertEqual(candidates[1]["relatedModules"][0]["moduleName"], "dialogos")

    def test_builds_analysis_repositories_from_primary_and_related_modules(self):
        routes, _, _, _, _ = load_routes_module()
        data = {
            "moduleId": "1",
            "branchAddress": "https://git.example/primary.git",
            "tagVersion": "v1",
            "relatedModules": [
                {
                    "moduleId": "2",
                    "moduleName": "dialogos",
                    "role": "related",
                    "branchAddress": "https://git.example/dialogos.git",
                    "tagVersion": "v2",
                }
            ],
        }

        repositories = routes.build_analysis_repositories(data, "task-123")

        self.assertEqual([item["role"] for item in repositories], ["primary", "related"])
        self.assertEqual(repositories[0]["branchAddress"], "https://git.example/primary.git")
        self.assertEqual(repositories[1]["moduleName"], "dialogos")
        self.assertEqual(repositories[1]["workspaceId"], "task-123")

    def test_clones_analysis_repositories_in_parallel(self):
        routes, _, _, _, _ = load_routes_module()
        repositories = [
            {"branchAddress": f"https://git.example/repo-{index}.git", "tagVersion": "v1", "workspaceId": "task"}
            for index in range(4)
        ]
        lock = threading.Lock()
        active = 0
        max_active = 0

        def fake_clone(address, tag_version, workspace_id=None):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return f"/tmp/{Path(address).stem}-{tag_version}"

        cloned = routes.clone_analysis_repositories(repositories, fake_clone, max_workers=4)

        self.assertEqual(len(cloned), 4)
        self.assertGreater(max_active, 1)

    def test_assesses_related_code_owner_from_selected_downstream_version(self):
        routes, _, _, _, _ = load_routes_module()
        with tempfile.TemporaryDirectory() as repo_dir:
            source_file = Path(repo_dir) / "DialogosCallbackClient.java"
            source_file.write_text(
                "\n".join([
                    "class DialogosCallbackClient {",
                    "  void notifyResult() { httpClient.post(\"/callback/result\"); }",
                    "}",
                ]),
                encoding="utf-8",
            )
            decision = routes.assess_chain_relevance("HTTP 500 callback response error")
            result = routes.assess_related_code_ownership(
                "HTTP 500 callback response error",
                decision,
                [{
                    "role": "downstream",
                    "moduleId": "2",
                    "moduleName": "dialogos",
                    "repo_path": repo_dir,
                }],
            )

        self.assertTrue(result["requiresRelatedEvidence"])
        self.assertEqual(result["chainOwner"], "downstream")
        self.assertIn("下游", result["message"])

    def test_build_code_findings_names_file_line_and_code_block(self):
        routes, _, _, _, _ = load_routes_module()
        findings = routes.build_code_findings([
            {
                "file": "src/main/java/demo/QualitySessionDataRemoveHandler.java",
                "line": 28,
                "start_line": 24,
                "end_line": 33,
                "numbered_snippet": "     27 | before();\n>>   28 | removeSession();",
                "snippet": "before();\nremoveSession();",
            }
        ])

        self.assertEqual(findings[0]["file"], "src/main/java/demo/QualitySessionDataRemoveHandler.java")
        self.assertEqual(findings[0]["line"], 28)
        self.assertIn("28", findings[0]["reason"])
        self.assertIn(">>   28 | removeSession();", findings[0]["code"])

    def test_finds_component_code_usages_for_redis_and_flyway_errors(self):
        routes, _, _, _, _ = load_routes_module()
        with tempfile.TemporaryDirectory() as repo_dir:
            redis_file = Path(repo_dir) / "RedisCacheService.java"
            redis_file.write_text(
                "\n".join([
                    "class RedisCacheService {",
                    "  private RedisTemplate redisTemplate;",
                    "  void save(String key) { redisTemplate.opsForValue().set(key, \"1\"); }",
                    "}",
                ]),
                encoding="utf-8",
            )
            flyway_file = Path(repo_dir) / "FlywayConfig.java"
            flyway_file.write_text(
                "\n".join([
                    "class FlywayConfig {",
                    "  Flyway flyway() {",
                    "    return Flyway.configure().baselineOnMigrate(true).load();",
                    "  }",
                    "}",
                ]),
                encoding="utf-8",
            )

            redis_components = routes.detect_error_components("Redis connection reset by peer")
            flyway_components = routes.detect_error_components("Flyway migration failed")
            redis_snippets = routes.find_component_code_usages(repo_dir, redis_components)
            flyway_snippets = routes.find_component_code_usages(repo_dir, flyway_components)

        self.assertIn("redis", redis_components)
        self.assertTrue(any("RedisCacheService.java" in item["file"] for item in redis_snippets))
        self.assertTrue(any("RedisTemplate" in item["numbered_snippet"] for item in redis_snippets))
        self.assertIn("flyway", flyway_components)
        self.assertTrue(any("FlywayConfig.java" in item["file"] for item in flyway_snippets))
        self.assertTrue(any("Flyway.configure" in item["numbered_snippet"] for item in flyway_snippets))

    def test_error_fingerprint_ignores_dynamic_ids_and_timestamps(self):
        routes, _, _, _, _ = load_routes_module()
        error_info = [{
            "file": "RedisClient.java",
            "line": 88,
            "error": "ConnectionResetError: userId=12345 requestId=abc-123 at 2026-06-13 15:30:01",
        }]

        first = routes.build_error_fingerprint("45", "251", error_info)
        error_info[0]["error"] = "ConnectionResetError: userId=67890 requestId=xyz-999 at 2026-06-14 16:31:02"
        second = routes.build_error_fingerprint("45", "251", error_info)

        self.assertEqual(first, second)

    def test_error_fingerprint_is_scoped_by_branch_version(self):
        routes, _, _, _, _ = load_routes_module()
        error_info = [{
            "file": "Demo.java",
            "line": 42,
            "error": "java.lang.NullPointerException: null",
        }]

        release_fingerprint = routes.build_error_fingerprint(
            "45",
            "251",
            error_info,
            "https://code.in.wezhuiyi.com/pal/pal-quality-inspection.git",
            "release_zyzx/1.3.2",
        )
        old_tag_fingerprint = routes.build_error_fingerprint(
            "45",
            "251",
            error_info,
            "https://code.in.wezhuiyi.com/pal/pal-quality-inspection.git",
            "v0.23.29-ZYKJ20220503001-rc1",
        )

        self.assertNotEqual(release_fingerprint, old_tag_fingerprint)

    def test_error_fingerprint_is_scoped_by_related_repository_versions(self):
        routes, _, _, _, _ = load_routes_module()
        error_info = [{
            "file": "Demo.java",
            "line": 42,
            "error": "java.lang.NullPointerException: null",
        }]
        base_args = (
            "45",
            "251",
            error_info,
            "https://code.in.wezhuiyi.com/see/see-task.git",
            "v2.10.1",
        )

        release_one = routes.build_error_fingerprint(
            *base_args,
            related_modules=[{
                "moduleName": "see-management",
                "role": "downstream",
                "branchAddress": "https://code.in.wezhuiyi.com/see/see-management.git",
                "tagVersion": "release-1",
            }],
        )
        release_two = routes.build_error_fingerprint(
            *base_args,
            related_modules=[{
                "moduleName": "see-management",
                "role": "downstream",
                "branchAddress": "https://code.in.wezhuiyi.com/see/see-management.git",
                "tagVersion": "release-2",
            }],
        )

        self.assertNotEqual(release_one, release_two)

    def test_extracts_related_repository_code_from_logged_api_path(self):
        routes, _, _, _, _ = load_routes_module()
        with tempfile.TemporaryDirectory() as repo_dir:
            controller = Path(repo_dir) / "DatasetController.java"
            controller.write_text(
                '@PostMapping("/facade/dataset/update-session")\n'
                'public void updateSession() {}\n',
                encoding="utf-8",
            )
            log_content = (
                "url=http://see-dataset-svc:9005/see-dataset/"
                "facade/dataset/update-session, queryString=null"
            )

            snippets = routes.find_related_repository_code_usages(
                repo_dir,
                log_content,
                components=[],
            )

            self.assertTrue(any("DatasetController.java" in item["file"] for item in snippets))
            self.assertTrue(any("update-session" in item["numbered_snippet"] for item in snippets))

    def test_parses_structured_issue_conclusion_from_ai_json(self):
        routes, _, _, _, _ = load_routes_module()
        ai_text = """
        ```json
        {
          "issue_category": "network_issue",
          "conclusion_summary": "Redis connection was reset.",
          "root_cause": "The broker closed the socket.",
          "solution": "Check Redis timeout and keepalive.",
          "query_commands": ["redis-cli -h 127.0.0.1 -p 6379 ping"],
          "fix_commands": ["redis-cli CONFIG SET timeout 300"],
          "confidence": 0.82,
          "possible_causes": {
            "code_issue": {"possible": false, "confidence": 0.2, "reason": "No code evidence"},
            "config_issue": {"possible": true, "confidence": 0.6, "reason": "Timeout may be too short"},
            "network_issue": {"possible": true, "confidence": 0.9, "reason": "Connection reset by peer"}
          },
          "evidence": ["Connection reset by peer"]
        }
        ```
        """

        conclusion = routes.parse_issue_conclusion(ai_text)

        self.assertEqual(conclusion["issue_category"], "network_issue")
        self.assertEqual(conclusion["issue_category_label"], "网络问题")
        self.assertEqual(conclusion["confidence"], 0.82)
        self.assertIn("Connection reset by peer", conclusion["evidence"])
        self.assertFalse(conclusion["possible_causes"]["code_issue"]["possible"])
        self.assertTrue(conclusion["possible_causes"]["config_issue"]["possible"])
        self.assertTrue(conclusion["possible_causes"]["network_issue"]["possible"])
        self.assertEqual(conclusion["possible_causes"]["network_issue"]["confidence"], 0.9)
        self.assertEqual(conclusion["query_commands"][0], "redis-cli -h 127.0.0.1 -p 6379 ping")
        self.assertEqual(conclusion["fix_commands"][0], "redis-cli CONFIG SET timeout 300")

    def test_parse_issue_conclusion_normalizes_command_lists(self):
        routes, _, _, _, _ = load_routes_module()

        conclusion = routes.parse_issue_conclusion(
            """
            {
              "issue_category": "config_issue",
              "query_commands": "grep -n 'timeout' application.yml",
              "fix_commands": ["sed -i 's/timeout: 30/timeout: 300/' application.yml", ""]
            }
            """
        )

        self.assertEqual(conclusion["query_commands"], ["grep -n 'timeout' application.yml"])
        self.assertEqual(conclusion["fix_commands"], ["sed -i 's/timeout: 30/timeout: 300/' application.yml"])

    def test_parse_issue_conclusion_normalizes_multiple_issues(self):
        routes, _, _, _, _ = load_routes_module()

        conclusion = routes.parse_issue_conclusion(
            """
            {
              "issue_category": "dependency_issue",
              "conclusion_summary": "Multiple failures were found.",
              "issues": [
                {
                  "title": "ASR audio download failed",
                  "issue_category": "dependency_issue",
                  "summary": "ASR could not download MinIO audio.",
                  "root_cause": "MinIO URL returned download failure.",
                  "solution": "Check object existence and bucket permission.",
                  "evidence": ["line 23 SeeTaskException", "line 88 Can not download file"],
                  "query_commands": "grep -n 'Can not download file' app.log",
                  "fix_commands": ["mc stat minio/zhuiyi-see/mock/audio.wav"],
                  "code_locations": [
                    {"file": "BaseAsrTransferService.java", "line": 226, "reason": "callback converts ASR failure"}
                  ]
                },
                {
                  "title": "Callback marked failed flow as success",
                  "issue_category": "code_issue",
                  "summary": "Callback log misleads the diagnosis.",
                  "evidence": ["line 44 callback handle success"],
                  "code_locations": [
                    {"file": "AsrTransformController.java", "line": 44}
                  ]
                }
              ]
            }
            """
        )

        self.assertEqual(len(conclusion["issues"]), 2)
        self.assertEqual(conclusion["issue_count"], 2)
        self.assertEqual(conclusion["issues"][0]["title"], "ASR audio download failed")
        self.assertEqual(conclusion["issues"][0]["query_commands"], ["grep -n 'Can not download file' app.log"])
        self.assertEqual(conclusion["issues"][0]["fix_commands"], ["mc stat minio/zhuiyi-see/mock/audio.wav"])
        self.assertEqual(conclusion["issues"][0]["code_locations"][0]["file"], "BaseAsrTransferService.java")
        self.assertEqual(conclusion["issues"][1]["issue_category"], "code_issue")

    def test_parse_issue_conclusion_accepts_issue_and_command_field_aliases(self):
        routes, _, _, _, _ = load_routes_module()
        conclusion = routes.parse_issue_conclusion(
            json.dumps({
                "issue_category": "dependency_issue",
                "conclusion_summary": "识别到两个独立问题",
                "issue_details": [
                    {
                        "title": "依赖下载失败",
                        "summary": "依赖包无法下载",
                        "fix_commond": ["pip install demo-package"],
                    },
                    {
                        "title": "服务连接失败",
                        "summary": "目标服务拒绝连接",
                        "query_command": "curl -v http://service/health",
                    },
                ],
            }, ensure_ascii=False)
        )

        self.assertEqual(conclusion["issue_count"], 2)
        self.assertEqual(conclusion["issues"][0]["fix_commands"], ["pip install demo-package"])
        self.assertEqual(conclusion["issues"][1]["query_commands"], ["curl -v http://service/health"])

    def test_parse_issue_conclusion_accepts_problem_details_and_singular_command_fields(self):
        routes, _, _, _, _ = load_routes_module()
        conclusion = routes.parse_issue_conclusion(
            json.dumps({
                "problem_details": [
                    {"title": "问题一", "fix_command": "command-1"},
                    {"title": "问题二", "query_commond": ["command-2"]},
                ]
            }, ensure_ascii=False)
        )

        self.assertEqual(conclusion["issue_count"], 2)
        self.assertEqual(conclusion["issues"][0]["fix_commands"], ["command-1"])
        self.assertEqual(conclusion["issues"][1]["query_commands"], ["command-2"])

    def test_image_prompt_requires_visible_errors_even_when_local_ocr_finds_none(self):
        routes, flask_stub, _, _, fake_openai = load_routes_module()
        flask_stub.current_app.config = {
            "OPENAI_KEY": "token",
            "OPENAI_URL": "https://example.invalid/v1",
            "OPENAI_MODEL": "gpt-test",
            "OPENAI_API_STYLE": "chat",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            image_paths = []
            for name in ("first.png", "second.png"):
                image_path = Path(temp_dir) / name
                image_path.write_bytes(b"fake-png")
                image_paths.append(str(image_path))
            routes.analyze_code_with_deepseek(
                "image_tag=business_image",
                "grouped_issue_count: 0\nNo explicit ERROR/Exception/FATAL/panic event was detected.",
                [],
                image_analysis={"image_tag": "business_image", "image_description": "包含两个错误的截图"},
                image_paths=image_paths,
            )

        prompt = fake_openai.last_instance.chat.completions.calls[0]["messages"][1]["content"]
        image_blocks = [item for item in prompt if isinstance(item, dict) and item.get("type") == "image_url"]
        self.assertEqual(len(image_blocks), 2)
        if isinstance(prompt, list):
            prompt = "\n".join(str(item.get("text") or "") for item in prompt if isinstance(item, dict))
        self.assertIn("不能因为图片识别未提取到文字", prompt)
        self.assertIn("逐个识别原图中所有可见的错误", prompt)
        self.assertIn("第1张、第2张", prompt)

    def test_extract_log_error_events_keeps_late_null_pointer_after_asr_stack(self):
        routes, _, _, _, _ = load_routes_module()
        stack_padding = "\n".join(
            f"    at org.springframework.demo.Filter{i}.doFilter(Filter{i}.java:{i})"
            for i in range(1, 32)
        )
        log_content = f"""
2026-06-24 17:44:45.900 ERROR 40 --- [io-9007-exec-18] c.z.s.t.s.asr.SessionAsrTransferService : [] 4-48-703 asr transform error

com.zhuiyi.see.task.exception.SeeTaskException: 调用离线asr转写语音失败,错误原因: Can not download file, please check url: http://minio-cluster:9000/zhuiyi-see/mock/dataset-schedule/audio/mock-audio-18.wav and disk space.
    at com.zhuiyi.see.task.service.asr.BaseAsrTransferService.handleCallback(BaseAsrTransferService.java:226)
    at com.zhuiyi.see.task.controller.AsrTransformController.asrCallback(AsrTransformController.java:44)
{stack_padding}
2026-06-24 18:01:37.228 INFO 40 --- [bot-check-3] c.z.s.t.service.impl.LLMCheckServiceImpl : complaint response
2026-06-24 18:01:37.332 INFO 40 --- [bot-check-3] c.z.s.t.service.impl.PromptServiceImpl : get system prompt: -2
2026-06-24 18:01:37.348 ERROR 40 --- [bot-check-3] c.z.s.t.s.impl.ComplaintAnalysisService : [] 4-28 complaint analysis error

java.lang.NullPointerException: null
    at com.zhuiyi.see.task.service.impl.ComplaintAnalysisService.doAnalyze(ComplaintAnalysisService.java:143)
    at com.zhuiyi.see.task.service.impl.ComplaintAnalysisService.analyze(ComplaintAnalysisService.java:95)
    at com.zhuiyi.see.task.service.impl.BotCheckTaskServiceImpl.checkOneTask(BotCheckTaskServiceImpl.java:1091)
"""

        events = routes.extract_log_error_events(log_content)
        summary = routes.build_log_error_event_summary(log_content)

        self.assertEqual(len(events), 2)
        self.assertIn("SeeTaskException", events[0]["text"])
        self.assertIn("BaseAsrTransferService.java:226", events[0]["text"])
        self.assertIn("NullPointerException", events[1]["text"])
        self.assertIn("ComplaintAnalysisService.java:143", events[1]["text"])
        self.assertIn("2", summary)

    def test_groups_duplicate_errors_but_keeps_late_independent_exception(self):
        routes, _, _, _, _ = load_routes_module()
        log_content = """2026-06-24 10:00:00 ERROR asr transform error
SeeTaskException: Can not download file
    at demo.Asr.run(Asr.java:10)
2026-06-24 10:00:01 ERROR asr transform error
SeeTaskException: Can not download file
    at demo.Asr.run(Asr.java:10)
2026-06-24 10:00:02 ERROR asr transform error
SeeTaskException: Can not download file
    at demo.Asr.run(Asr.java:10)
2026-06-24 10:05:00 ERROR complaint analysis error
java.lang.NullPointerException: null
    at demo.Complaint.run(Complaint.java:42)
"""

        grouped = routes.group_log_error_events(routes.extract_log_error_events(log_content))

        self.assertEqual(len(grouped), 2)
        self.assertEqual(grouped[0]["occurrence_count"], 3)
        self.assertIn("NullPointerException", grouped[1]["representative_text"])
        self.assertLess(grouped[0]["first_line"], grouped[1]["first_line"])

    def test_error_fingerprint_includes_every_group_signature(self):
        routes, _, _, _, _ = load_routes_module()
        first_error = [{"error": "SeeTaskException: ASR download failed", "file": "AsrService.java"}]
        asr_group = {"signature": "seetaskexception|asr download failed|java:AsrService.java:226"}
        npe_group = {"signature": "nullpointerexception|null|java:ComplaintService.java:143"}

        asr_only = routes.build_error_fingerprint(
            "46", "256", first_error, grouped_log_errors=[asr_group]
        )
        asr_and_npe = routes.build_error_fingerprint(
            "46", "256", first_error, grouped_log_errors=[asr_group, npe_group]
        )

        self.assertNotEqual(asr_only, asr_and_npe)

    def test_error_extraction_ignores_error_level_success_records(self):
        routes, _, _, _, _ = load_routes_module()
        log_content = "\n".join([
            '2026-06-24 17:57:05.761 ERROR GraphEngineHandler : 打标结果为 message: "success"',
            "2026-06-24 18:01:37.348 ERROR ComplaintAnalysisService : complaint analysis error",
            "java.lang.NullPointerException: null",
            "\tat demo.ComplaintService.run(ComplaintService.java:143)",
        ])

        events = routes.extract_log_error_events(log_content)

        self.assertEqual(len(events), 1)
        self.assertIn("NullPointerException", events[0]["text"])

    def test_backend_summary_represents_every_group_with_independent_budget(self):
        routes, _, _, _, _ = load_routes_module()
        large_asr_stack = "\n".join(
            f"    at demo.Filter{index}.run(Filter{index}.java:{index})"
            for index in range(1, 60)
        )
        log_content = f"""2026-06-24 10:00:00 ERROR ASR transform error
SeeTaskException: Can not download file
{large_asr_stack}
2026-06-24 10:05:00 ERROR complaint analysis error
java.lang.NullPointerException: null
    at demo.Complaint.run(Complaint.java:42)
"""

        summary = routes.build_backend_log_analysis(log_content, max_chars=1800)

        self.assertIn("ASR", summary)
        self.assertIn("NullPointerException", summary)
        self.assertIn("occurrence_count", summary)

    def test_full_file_scan_keeps_error_after_more_than_one_hundred_twenty_events(self):
        routes, _, _, _, _ = load_routes_module()
        repeated = "\n".join(
            f"""2026-06-24 10:{index // 60:02d}:{index % 60:02d} ERROR ASR transform error
SeeTaskException: Can not download file
    at demo.Asr.run(Asr.java:10)"""
            for index in range(125)
        )
        log_content = repeated + """
2026-06-24 12:30:00 ERROR complaint analysis error
java.lang.NullPointerException: null
    at demo.Complaint.run(Complaint.java:42)
"""

        events = routes.extract_log_error_events(log_content)
        grouped = routes.group_log_error_events(events)

        self.assertEqual(len(events), 126)
        self.assertEqual(len(grouped), 2)
        self.assertIn("NullPointerException", grouped[-1]["representative_text"])

    def test_issue_category_labels_are_readable_chinese(self):
        routes, _, _, _, _ = load_routes_module()

        self.assertEqual(routes.ISSUE_CATEGORY_LABELS["code_issue"], "代码问题")
        self.assertEqual(routes.ISSUE_CATEGORY_LABELS["data_issue"], "数据问题")
        self.assertEqual(routes.ISSUE_CATEGORY_LABELS["config_issue"], "配置问题")
        self.assertEqual(routes.ISSUE_CATEGORY_LABELS["network_issue"], "网络问题")
        self.assertEqual(routes.ISSUE_CATEGORY_LABELS["resource_issue"], "资源问题")
        self.assertEqual(routes.ISSUE_CATEGORY_LABELS["unknown"], "未知")

    def test_parse_issue_conclusion_falls_back_to_unknown(self):
        routes, _, _, _, _ = load_routes_module()

        conclusion = routes.parse_issue_conclusion("plain text result")

        self.assertEqual(conclusion["issue_category"], "unknown")
        self.assertEqual(conclusion["confidence"], 0)
        self.assertEqual(conclusion["raw_analysis"], "plain text result")


if __name__ == "__main__":
    unittest.main()
