import importlib.util
import os
import tempfile
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


TASKS_PATH = Path(__file__).resolve().parents[1] / "app" / "analysis" / "routes" / "tasks.py"


class FakeCelery:
    def task(self, *args, **kwargs):
        return lambda func: func


class FakeTask:
    def __init__(self):
        self.states = []

    def update_state(self, state=None, meta=None):
        self.states.append({"state": state, "meta": meta})


def load_tasks_module():
    app_stub = types.ModuleType("app")
    config_stub = types.ModuleType("app.config")
    config_stub.DEFAULT_REPO_CACHE_DIR = "/tmp/jira-automation-repos"
    app_stub.config = config_stub
    extensions_stub = types.ModuleType("extensions")
    extensions_stub.celery = FakeCelery()
    stub_modules = {
        "app": app_stub,
        "app.config": config_stub,
        "extensions": extensions_stub,
    }
    previous_modules = {name: sys.modules.get(name) for name in stub_modules}
    try:
        sys.modules.update(stub_modules)
        spec = importlib.util.spec_from_file_location("analysis_tasks_under_test", TASKS_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, previous in previous_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


class AnalysisTaskProgressTests(unittest.TestCase):
    def test_async_task_uses_backend_summary_instead_of_preliminary_ai(self):
        source = TASKS_PATH.read_text(encoding="utf-8")

        self.assertNotIn("analyze_log_with_deepseek", source)
        self.assertNotIn("analyze_uploaded_image", source)
        self.assertIn("build_backend_log_analysis", source)

    def test_image_ocr_text_becomes_log_content_without_repeating_ocr(self):
        """显式开启本地 OCR 时，前端回传的 OCR 文本直接复用，不重复识别。"""
        module = load_tasks_module()
        calls = []
        supplied_ocr = {
            "available": True,
            "engine": "paddleocr",
            "extracted_text": (
                "2026-07-14 ERROR complaint analysis error\n"
                "java.lang.NullPointerException: null\n"
                "at demo.Service.run(Service.java:42)"
            ),
            "lines": [],
            "average_confidence": 0.97,
            "warnings": [],
        }

        with mock.patch.dict("os.environ", {"LOCAL_OCR_ENABLED": "true"}, clear=False):
            log_content, result = module.build_image_ocr_log_content(
                {
                    "image_tag": "log_image",
                    "image_description": "空指针截图",
                    "image_ocr": supplied_ocr,
                },
                ["/tmp/error.png"],
                ocr_extractor=lambda paths: calls.append(paths),
            )

        self.assertEqual(calls, [])
        self.assertIn("NullPointerException", log_content)
        self.assertIn("空指针截图", log_content)
        self.assertEqual(result["average_confidence"], 0.97)

    def test_image_task_uses_model_by_default_without_local_ocr(self):
        """默认停用本地 OCR：任务侧直接走模型识别，本地 OCR 通道不会被调用。"""
        module = load_tasks_module()
        local_calls = []

        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("LOCAL_OCR_ENABLED", None)
            log_content, result = module.build_image_ocr_log_content(
                {
                    "image_tag": "log_image",
                    "image_description": "空指针截图",
                },
                ["/tmp/error.png"],
                ocr_extractor=lambda paths: local_calls.append(paths),
                image_fallback_extractor=lambda paths, image_tag, image_description: {
                    "summary": "模型识别摘要",
                    "extracted_text": "java.lang.NullPointerException",
                    "keywords": ["nullpointer"],
                    "components": ["api"],
                },
            )

        self.assertEqual(local_calls, [])
        self.assertIn("NullPointerException", log_content)
        self.assertEqual(result["engine"], "gpt-vision")
        self.assertTrue(result["available"])

    def test_image_ocr_can_fall_back_to_gpt_when_local_ocr_is_disabled(self):
        module = load_tasks_module()
        fallback_calls = []

        with mock.patch.dict("os.environ", {"LOCAL_OCR_ENABLED": "false"}, clear=False):
            log_content, result = module.build_image_ocr_log_content(
                {
                    "image_tag": "log_image",
                    "image_description": "模糊截图",
                    "image_ocr": {
                        "available": False,
                        "engine": "paddleocr",
                        "extracted_text": "",
                        "lines": [],
                        "average_confidence": 0.0,
                        "warnings": ["local OCR disabled"],
                    },
                },
                ["/tmp/error.png"],
                ocr_extractor=lambda paths: fallback_calls.append(paths),
                image_fallback_extractor=lambda paths, image_tag, image_description: {
                    "summary": "gpt summary",
                    "extracted_text": "GPT extracted text",
                    "keywords": ["error"],
                    "components": ["api"],
                },
            )

        self.assertEqual(fallback_calls, [])
        self.assertIn("GPT extracted text", log_content)
        self.assertEqual(result["engine"], "gpt-vision")
        self.assertTrue(result["available"])

    def test_image_task_runs_ocr_before_full_error_extraction(self):
        source = TASKS_PATH.read_text(encoding="utf-8")

        ocr_position = source.index("build_image_ocr_log_content(", source.index("def analyze_log_task"))
        event_position = source.index("extract_log_error_events(log_content)", ocr_position)
        backend_analysis_position = source.index("build_backend_log_analysis(analysis_log_content)", event_position)

        self.assertLess(ocr_position, event_position)
        self.assertLess(event_position, backend_analysis_position)

    def test_image_tasks_never_reuse_log_knowledge_cache(self):
        module = load_tasks_module()

        self.assertFalse(module.should_use_knowledge_cache("image"))
        self.assertTrue(module.should_use_knowledge_cache("file"))

        source = TASKS_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "knowledge_case = find_knowledge_case", source
        )
        self.assertIn(
            "if should_use_knowledge_cache(source_type)", source
        )

    def test_report_progress_updates_celery_meta_with_stage_message(self):
        module = load_tasks_module()
        task = FakeTask()

        module.report_progress(
            task,
            stage="clone_repo_done",
            stage_label="代码拉取",
            stage_percent=100,
            percent=45,
        )

        self.assertEqual(task.states[0]["state"], "PROGRESS")
        self.assertEqual(task.states[0]["meta"]["stage"], "clone_repo_done")
        self.assertEqual(task.states[0]["meta"]["stage_label"], "代码拉取")
        self.assertEqual(task.states[0]["meta"]["stage_percent"], 100)
        self.assertEqual(task.states[0]["meta"]["percent"], 45)
        self.assertEqual(task.states[0]["meta"]["message"], "代码拉取 100%")

    def test_task_progress_names_the_deep_fault_reasoning_stage(self):
        source = TASKS_PATH.read_text(encoding="utf-8")

        self.assertIn('"deep_reasoning_started"', source)
        self.assertIn('"深度推理"', source)
        self.assertIn("正在使用 gpt-5.6-sol 进行深度故障推理", source)

    def test_build_analysis_evidence_marks_when_code_context_was_used(self):
        module = load_tasks_module()
        error_info = [{"file": "Demo.java", "line": 12, "error": "boom"}]
        resolved_errors = [{"file": "/repo/Demo.java", "line": 12, "error": "boom"}]
        code_snippets = [{"file": "/repo/Demo.java", "line": 12, "snippet": "throw boom;"}]

        evidence = module.build_analysis_evidence(
            error_info,
            resolved_errors,
            code_snippets,
            log_error_events=[{"line": 10}, {"line": 42}],
        )

        self.assertTrue(evidence["used_code_context"])
        self.assertEqual(evidence["error_info_count"], 1)
        self.assertEqual(evidence["log_error_event_count"], 2)
        self.assertEqual(evidence["resolved_file_count"], 1)
        self.assertEqual(evidence["code_snippet_count"], 1)
        self.assertEqual(evidence["code_snippet_files"], ["/repo/Demo.java"])

    def test_build_analysis_evidence_distinguishes_selected_and_used_related_code(self):
        module = load_tasks_module()
        snippets = [
            {
                "file": "/repos/primary/Demo.java",
                "line": 12,
                "snippet": "run();",
                "module_name": "see-task",
                "module_role": "primary",
            },
            {
                "file": "/repos/downstream/DatasetController.java",
                "line": 20,
                "snippet": "updateSession();",
                "module_name": "see-dataset",
                "module_role": "downstream",
            },
        ]
        repositories = [
            {"moduleName": "see-task", "role": "primary", "tagVersion": "v2"},
            {"moduleName": "see-dataset", "role": "downstream", "tagVersion": "release-1"},
        ]

        evidence = module.build_analysis_evidence(
            error_info=[],
            resolved_errors=[],
            code_snippets=snippets,
            repositories=repositories,
        )

        self.assertTrue(evidence["used_code_context"])
        self.assertTrue(evidence["used_related_code_context"])
        self.assertEqual(evidence["related_code_module_count"], 1)
        self.assertEqual(evidence["code_context_modules"][1]["moduleName"], "see-dataset")
        self.assertEqual(evidence["selected_code_repositories"][1]["tagVersion"], "release-1")

    def test_build_analysis_evidence_includes_auto_pulled_common_module_metadata(self):
        module = load_tasks_module()
        references = [{
            "moduleName": "bot-common",
            "branchAddress": "https://git.example/bot-common.git",
            "matchedBy": "code",
            "file": "pom.xml",
            "line": 18,
        }]
        common_repositories = [{
            "moduleName": "bot-common",
            "branchAddress": "https://git.example/bot-common.git",
            "role": "common",
            "common": True,
            "repo_path": "/repos/bot-common-default",
            "autoPulled": True,
        }]
        skipped = [{
            "moduleName": "wenben-filebeat",
            "branchAddress": "https://git.example/wenben-filebeat.git",
            "reason": "公共组件仓库拉取失败：network denied",
        }]

        evidence = module.build_analysis_evidence(
            error_info=[],
            resolved_errors=[],
            code_snippets=[],
            repositories=common_repositories,
            common_module_references=references,
            auto_pulled_common_modules=common_repositories,
            common_pull_skipped=skipped,
        )

        self.assertEqual(evidence["common_module_reference_count"], 1)
        self.assertEqual(evidence["common_module_references"], references)
        self.assertEqual(evidence["auto_pulled_common_module_count"], 1)
        self.assertEqual(evidence["auto_pulled_common_modules"], common_repositories)
        self.assertEqual(evidence["common_pull_skipped_count"], 1)
        self.assertEqual(evidence["common_pull_skipped"], skipped)
        self.assertTrue(evidence["selected_code_repositories"][0]["common"])
        self.assertTrue(evidence["selected_code_repositories"][0]["autoPulled"])

    def test_build_analysis_evidence_includes_group_language_and_ai_call_metadata(self):
        module = load_tasks_module()

        evidence = module.build_analysis_evidence(
            error_info=[{"file": "Demo.java", "line": 12, "language": "java"}],
            resolved_errors=[],
            code_snippets=[],
            log_error_events=[{"line": 10}, {"line": 20}, {"line": 42}],
            grouped_log_errors=[{"signature": "asr"}, {"signature": "npe"}],
            detected_languages=["java", "shell"],
            ai_call_count=1,
        )

        self.assertEqual(evidence["grouped_issue_count"], 2)
        self.assertEqual(evidence["grouped_log_errors"], [{"signature": "asr"}, {"signature": "npe"}])
        self.assertEqual(evidence["detected_languages"], ["java", "shell"])
        self.assertEqual(evidence["ai_call_count"], 1)

    def test_build_analysis_evidence_marks_when_code_context_is_missing(self):
        module = load_tasks_module()

        evidence = module.build_analysis_evidence(
            error_info=[{"file": "Missing.java", "line": 12, "error": "boom"}],
            resolved_errors=[],
            code_snippets=[],
        )

        self.assertFalse(evidence["used_code_context"])
        self.assertEqual(evidence["resolved_file_count"], 0)
        self.assertEqual(evidence["code_snippet_count"], 0)

    def test_build_skipped_chain_issue_context_keeps_issue_in_analysis_scope(self):
        module = load_tasks_module()

        context = module.build_skipped_chain_issue_context([{
            "issueSummary": "asr transform error",
            "reason": "用户跳过补充条件",
            "relatedModules": [{"moduleName": "asr"}],
        }])

        self.assertIn("asr transform error", context)
        self.assertIn("asr", context)
        self.assertIn("原始日志中的该异常仍必须参与最终故障分析", context)

    def test_cleanup_analysis_files_removes_only_task_upload_and_repo_workspace(self):
        module = load_tasks_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            upload_dir = os.path.join(temp_dir, "upload")
            repo_base_dir = os.path.join(temp_dir, "repos")
            task_id = "task-123"
            os.makedirs(upload_dir)
            os.makedirs(os.path.join(repo_base_dir, task_id, "demo-repo"))

            uploaded_file = os.path.join(upload_dir, "demo.log")
            repo_file = os.path.join(repo_base_dir, task_id, "demo-repo", "Demo.java")
            outside_file = os.path.join(temp_dir, "outside.log")
            Path(uploaded_file).write_text("log", encoding="utf-8")
            Path(repo_file).write_text("class Demo {}", encoding="utf-8")
            Path(outside_file).write_text("keep", encoding="utf-8")

            result = module.cleanup_analysis_files(
                {"file_path": uploaded_file},
                repo_path=os.path.join(repo_base_dir, task_id, "demo-repo"),
                task_id=task_id,
                upload_dir=upload_dir,
                repo_base_dir=repo_base_dir,
            )

            self.assertTrue(result["uploaded_file_removed"])
            self.assertTrue(result["repo_workspace_removed"])
            self.assertFalse(os.path.exists(uploaded_file))
            self.assertFalse(os.path.exists(os.path.join(repo_base_dir, task_id)))
            self.assertTrue(os.path.exists(outside_file))

    def test_cleanup_analysis_files_refuses_paths_outside_allowed_roots(self):
        module = load_tasks_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            upload_dir = os.path.join(temp_dir, "upload")
            repo_base_dir = os.path.join(temp_dir, "repos")
            os.makedirs(upload_dir)
            os.makedirs(repo_base_dir)

            outside_file = os.path.join(temp_dir, "outside.log")
            outside_repo = os.path.join(temp_dir, "outside-repo")
            Path(outside_file).write_text("keep", encoding="utf-8")
            os.makedirs(outside_repo)

            result = module.cleanup_analysis_files(
                {"file_path": outside_file},
                repo_path=outside_repo,
                task_id="task-123",
                upload_dir=upload_dir,
                repo_base_dir=repo_base_dir,
            )

            self.assertFalse(result["uploaded_file_removed"])
            self.assertFalse(result["repo_workspace_removed"])
            self.assertTrue(os.path.exists(outside_file))
            self.assertTrue(os.path.exists(outside_repo))

    def test_cleanup_analysis_files_removes_task_workspace_when_primary_clone_failed(self):
        module = load_tasks_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_base_dir = os.path.join(temp_dir, "repos")
            task_id = "task-failed-clone"
            task_workspace = os.path.join(repo_base_dir, task_id)
            os.makedirs(os.path.join(task_workspace, "related-repo"))

            result = module.cleanup_analysis_files(
                {},
                repo_path=None,
                task_id=task_id,
                upload_dir=os.path.join(temp_dir, "upload"),
                repo_base_dir=repo_base_dir,
            )

            self.assertTrue(result["repo_workspace_removed"])
            self.assertFalse(os.path.exists(task_workspace))


if __name__ == "__main__":
    unittest.main()
