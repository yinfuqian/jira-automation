"""缺陷单门禁技能（jira-gate-bug）测试：覆盖技能发现、交付物契约与脚本行为（HTML 报告、自查结果判定）。"""
import json
import http.server
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

SKILLS_DIR = BACKEND_DIR.parent / "skills"
SKILL_DIR = SKILLS_DIR / "jira-gate-bug"
NODE_BIN = shutil.which("node")


STUBBED_TOP_LEVEL_MODULES = ("app", "flask", "openai", "celery", "extensions")


def purge_stubbed_modules():
    """剔除其他测试模块遗留在 sys.modules 中的桩模块，保证导入真实实现。

    仓库里部分用例用 types.ModuleType 顶替 app/flask 等模块且不做还原，
    unittest discover 到本模块时 sys.modules 已被污染，因此导入前先清理。
    """
    for name in list(sys.modules):
        if name.split(".")[0] not in STUBBED_TOP_LEVEL_MODULES:
            continue
        module = sys.modules.get(name)
        if module is None:
            continue
        if getattr(module, "__file__", None):
            continue
        sys.modules.pop(name, None)



class DefectGateSkillDefinitionTests(unittest.TestCase):
    def test_skill_is_discoverable_with_runtime_contract(self):
        """技能目录可被发现，runtime.json 声明了提示词与必填输入。"""
        purge_stubbed_modules()

        from app.skillrun.registry import list_skills

        skills = {skill.skill_id: skill for skill in list_skills(SKILLS_DIR)}

        self.assertIn("jira-gate-bug", skills)
        skill = skills["jira-gate-bug"]
        self.assertTrue(skill.description)
        self.assertEqual(skill.required_inputs, ("jira_url",))
        self.assertEqual(skill.timeout_seconds, 3600)
        self.assertIn("analysis-cli.mjs", skill.prompt_template)
        self.assertIn("jira-cli.mjs", skill.prompt_template)
        self.assertIn("必须且只能使用", skill.prompt_template)
        self.assertIn("任何其它技能", skill.prompt_template)
        self.assertIn("不得仅凭 ANALYSIS_API_TOKEN 为空", skill.prompt_template)

    def test_skill_documents_the_whole_flow(self):
        """SKILL.md 必须覆盖完整流程，避免遗漏任一判定分支。"""
        content = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

        for required in (
            "完整性检查",
            "完整性检查必须同时检查可辨认图片",
            "模块映射丢失，已跳过检查",
            "scope-mapping-missing",
            "[~yinfuqian1193]",
            "打回",
            "自查结果检查",
            "自动故障分析",
            "analysis-cli.mjs",
            "jira-cli.mjs",
            "HTML",
            "gate-set",
            "attach",
            "jira-cli.mjs assign",
            "ANALYSIS_API_TOKEN",
            "rejectTransitionName",
        ):
            self.assertIn(required, content)

    def test_skill_documents_loopback_analysis_without_token(self):
        """说明文件必须明确同机回环免令牌，避免模型在发请求前自行阻断。"""
        content = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("同机回环内部调用免令牌", content)
        self.assertIn("不得仅凭 `ANALYSIS_API_TOKEN` 为空", content)
        self.assertNotIn("未配置时脚本会直接报错", content)

    def test_references_define_judgement_rules(self):
        """判定依据必须落在参考资料里，而不是让模型自行发挥。"""
        checklist = (SKILL_DIR / "references" / "checklist.md").read_text(encoding="utf-8")
        self_check = (SKILL_DIR / "references" / "self-check.md").read_text(encoding="utf-8")
        jira_api = (SKILL_DIR / "references" / "jira-api.md").read_text(encoding="utf-8")

        for required in (
            "MUST",
            "REDLINE",
            "效果类缺陷",
            "复现步骤",
            "时间信息",
            "日志",
            "明确缺陷不需要额外提供日志信息",
            "可辨认图片",
            "可辨认图片中的模块/组件",
            "不要求修复版本",
            "不要求环境入口或操作版本",
            "mappingOwner",
        ):
            self.assertIn(required, checklist)
        self.assertNotIn("含时区", checklist)
        self.assertNotIn("补入口、操作、数据", (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))
        for required in ("自查", "结论", "证据", "selfCheckKeywords"):
            self.assertIn(required, self_check)
        for required in ("/rest/api/2/issue/{KEY}/comment", "/attachments", "transitions"):
            self.assertIn(required, jira_api)

    def test_cli_entrypoints_expose_required_commands(self):
        """两个命令行入口必须暴露流程需要的子命令与参数。"""
        jira_cli = (SKILL_DIR / "scripts" / "jira-cli.mjs").read_text(encoding="utf-8")
        analysis_cli = (SKILL_DIR / "scripts" / "analysis-cli.mjs").read_text(encoding="utf-8")

        for command in ("selfcheck", "log-exemption", "scope", "attachments", "comment", "attach", "gate-set", "transitions", "assign"):
            self.assertIn('"' + command + '"', jira_cli)
        for option in ("--dir", "--out", "--product", "--module", "--json-summary"):
            self.assertIn(option, analysis_cli)
        self.assertIn("buildScopeFailurePayload", analysis_cli)
        self.assertNotIn("process.exit(3)", analysis_cli)

    def test_scope_mapping_covers_expected_products(self):
        """分析范围配置必须覆盖 Jira 问题发现产品的常用顶层组。"""
        mapping = json.loads((SKILL_DIR / "config" / "scope-mapping.json").read_text(encoding="utf-8"))

        for product in ("see", "pal", "bot", "yicall", "yibot", "eco", "hxk", "falcon", "aiforce"):
            self.assertIn(product, mapping["products"])
        self.assertEqual(
            mapping["products"]["aiforce"]["modules"]["aiforce2"]["repoUrl"],
            "https://code.in.wezhuiyi.com/aiforce/aiforce2.git",
        )
        self.assertIn(
            "aiforce-authentication",
            mapping["products"]["aiforce"]["modules"]["aiforce2"]["aliases"],
        )
        self.assertEqual(
            mapping["products"]["aiforce"]["modules"]["aiforce-web"]["repoUrl"],
            "https://code.in.wezhuiyi.com/aiforce/aiforce2.git",
        )
        self.assertEqual(
            mapping["products"]["see"]["modules"]["see-task"]["repoUrl"],
            "https://code.in.wezhuiyi.com/see/see-task.git",
        )
        self.assertEqual(mapping["mappingOwner"]["name"], "yinfuqian1193")
        self.assertEqual(mapping["mappingOwner"]["displayName"], "尹甫乾")
        self.assertEqual(mapping["mappingOwner"]["mention"], "[~yinfuqian1193]")
        common_modules = [
            (product, module)
            for product, config in mapping["products"].items()
            for module, item in config.get("modules", {}).items()
            if item.get("common")
        ]
        self.assertEqual(len(common_modules), 13)
        self.assertIn(("bot", "bot-common"), common_modules)
        self.assertIn(("hxk", "xxl-job"), common_modules)

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能校验技能脚本")
    def test_scripts_are_valid_node_modules(self):
        """技能脚本必须是可解析的 ES 模块，避免部署后才暴露语法错误。"""
        for name in ("jira.mjs", "jira-cli.mjs", "scope.mjs", "analysis.mjs", "analysis-cli.mjs", "report.mjs"):
            result = subprocess.run(
                [NODE_BIN, "--check", str(SKILL_DIR / "scripts" / name)],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(result.returncode, 0, f"{name} 语法校验失败：{result.stderr}")


class DefectGateScriptBehaviourTests(unittest.TestCase):
    """直接执行技能脚本，验证报告渲染与自查结果判定的真实行为。"""

    SAMPLE_PAYLOAD = {
        "issueKey": "KEY-1234",
        "jiraUrl": "https://jira.in.wezhuiyi.com/browse/KEY-1234",
        "scope": {"productName": "客户服务", "moduleName": "工单管理", "tagVersion": "v3.2.1"},
        "files": [{"filename": "server.log", "kind": "日志", "size": 2048}],
        "tasks": [
            {
                "label": "日志分析",
                "taskId": "task-1",
                "result": {
                    "analysis_evidence": {"code_snippet_count": 1, "grouped_log_errors": [{"signature": "timeout", "count": 3}]},
                    "issue_conclusion": {
                        "issue_category_label": "代码问题",
                        "conclusion_summary": "回调超时 <script>alert(1)</script>",
                        "confidence": 0.62,
                        "issue_count": 1,
                        "issues": [{"title": "回调超时", "root_cause": "未设置超时", "confidence": 0.6}],
                    },
                },
            }
        ],
    }

    def run_node(self, driver, *args):
        """在仓库目录内执行一段 ESM 驱动代码并返回 stdout。"""
        result = subprocess.run(
            [NODE_BIN, "--input-type=module", "-e", driver, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(BACKEND_DIR.parent),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_scope_mapping_resolves_repo_and_version_rule(self):
        """产品+模块必须解析为仓库，版本必须按规则转换成 Git Tag。"""
        scope_url = (SKILL_DIR / "scripts" / "scope.mjs").as_uri()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "scope-mapping.json"
            config_path.write_text(json.dumps({
                "versionRules": [
                    {"name": "semver-release", "pattern": "^([0-9]+\\.[0-9]+\\.[0-9]+)$", "ref": "release-{1}"}
                ],
                "products": {
                    "see": {
                        "modules": {
                            "see-task": {"repoUrl": "https://code.in.wezhuiyi.com/see/see-task.git"}
                        }
                    }
                },
            }, ensure_ascii=False), encoding="utf-8")
            driver = """
const { resolveConfiguredScope } = await import("__SCOPE__");
console.log(JSON.stringify(resolveConfiguredScope(
  { product: "see", module: "see-task", version: "3.2.1" },
  { configPath: process.argv[1] },
)));
""".replace("__SCOPE__", scope_url)

            output = self.run_node(driver, str(config_path))

        payload = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(payload["repoUrl"], "https://code.in.wezhuiyi.com/see/see-task.git")
        self.assertEqual(payload["gitTag"], "release-3.2.1")
        self.assertEqual(payload["rule"], "semver-release")

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_scope_mapping_prefers_exact_version_map(self):
        """精确版本映射必须优先于正则规则，便于维护非同名发布 Tag。"""
        scope_url = (SKILL_DIR / "scripts" / "scope.mjs").as_uri()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "scope-mapping.json"
            config_path.write_text(json.dumps({
                "versionRules": [
                    {"name": "exact-ref", "pattern": "^.+$", "ref": "{version}"}
                ],
                "products": {
                    "yicall": {
                        "modules": {
                            "yicall-task": {
                                "repoUrl": "https://code.in.wezhuiyi.com/yicall/yicall-task.git",
                                "versionMap": {"6.1.15": "v6.1.15-CallZhaoshangzhengquan-rc33"}
                            }
                        }
                    }
                },
            }, ensure_ascii=False), encoding="utf-8")
            driver = """
const { resolveConfiguredScope } = await import("__SCOPE__");
console.log(JSON.stringify(resolveConfiguredScope(
  { product: "yicall", module: "yicall-task", version: "6.1.15" },
  { configPath: process.argv[1] },
)));
""".replace("__SCOPE__", scope_url)

            output = self.run_node(driver, str(config_path))

        payload = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(payload["gitTag"], "v6.1.15-CallZhaoshangzhengquan-rc33")
        self.assertEqual(payload["rule"], "version-map:6.1.15")

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_missing_scope_mapping_skips_check_and_mentions_mapping_owner(self):
        """工单已有模块但配置缺映射时，必须通知维护人并跳过检查，不能按缺参打回。"""
        scope_url = (SKILL_DIR / "scripts" / "scope.mjs").as_uri()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "scope-mapping.json"
            config_path.write_text(json.dumps({
                "versionRules": [{"name": "exact-tag", "pattern": "^.+$", "ref": "{version}"}],
                "products": {
                    "see": {
                        "modules": {
                            "see-task": {"repoUrl": "https://code.in.wezhuiyi.com/see/see-task.git"}
                        }
                    }
                },
            }, ensure_ascii=False), encoding="utf-8")
            driver = """
const {
  buildScopeFailurePayload,
  resolveConfiguredScope,
} = await import("__SCOPE__");
let failure = null;
try {
  resolveConfiguredScope(
    { product: "see", module: "operation-task", version: "v2.3.73-update" },
    { configPath: process.argv[1] },
  );
} catch (error) {
  failure = buildScopeFailurePayload(error, process.argv[1]);
}
console.log(JSON.stringify(failure));
""".replace("__SCOPE__", scope_url)

            output = self.run_node(driver, str(config_path))

        payload = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(payload["reason"], "scope-mapping-missing")
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["gateResult"], "未检查")
        self.assertEqual(payload["mappingOwner"]["mention"], "[~yinfuqian1193]")
        self.assertIn("模块映射丢失，已跳过检查", payload["comment"])
        self.assertIn("[~yinfuqian1193]", payload["comment"])
        self.assertIn("补齐映射内容", payload["comment"])
        self.assertIn("operation-task", payload["comment"])
        self.assertIn("本单会转派给上述映射维护人", payload["comment"])
        self.assertIn("不改变工作流状态", payload["comment"])
        self.assertNotIn("打回", payload["comment"])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_cli_mapping_gap_exits_zero_so_agent_can_post_comment(self):
        """映射缺口是正常分流结果，CLI 必须退出 0，避免 Agent 停在失败状态而不写评论。"""
        class FakeAnalysisHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定方法名
                if self.path.startswith("/product/get"):
                    body = json.dumps({"products": [{"id": 1, "name": "see"}]}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, format, *args):  # noqa: A002 - 基类签名固定
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAnalysisHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "scope-mapping.json"
                config_path.write_text(json.dumps({
                    "versionRules": [{"name": "exact-tag", "pattern": "^.+$", "ref": "{version}"}],
                    "products": {"see": {"modules": {}}},
                }, ensure_ascii=False), encoding="utf-8")
                result = subprocess.run(
                    [
                        NODE_BIN,
                        str(SKILL_DIR / "scripts" / "analysis-cli.mjs"),
                        "resolve",
                        "--product", "see",
                        "--module", "operation-task",
                        "--version", "v2.3.73-update",
                        "--config", str(config_path),
                        "--base-url", f"http://127.0.0.1:{server.server_port}",
                        "--token", "test-token",
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    cwd=str(BACKEND_DIR.parent),
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "scope-mapping-missing")
        self.assertEqual(payload["gateResult"], "未检查")
        self.assertIn("[~yinfuqian1193]", payload["comment"])
        self.assertIn("operation-task", payload["comment"])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_analysis_api_accepts_port_rejected_by_node_fetch(self):
        """故障分析服务使用 6000 端口时，不能因 Node fetch 的 unsafe-port 限制而误报连接失败。"""
        class FakeAnalysisHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定方法名
                if self.path.startswith("/product/get"):
                    body = json.dumps({"products": [{"id": 1, "name": "Aiforce"}]}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, format, *args):  # noqa: A002 - 基类签名固定
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 6000), FakeAnalysisHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = subprocess.run(
                [
                    NODE_BIN,
                    str(SKILL_DIR / "scripts" / "analysis-cli.mjs"),
                    "selftest",
                    "--base-url", "http://127.0.0.1:6000",
                    "--token", "test-token",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=str(BACKEND_DIR.parent),
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["baseUrl"], "http://127.0.0.1:6000")
        self.assertEqual(payload["steps"][-1]["step"], "访问故障分析服务")
        self.assertTrue(payload["steps"][-1]["ok"])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_loopback_analysis_selftest_does_not_require_internal_token(self):
        """同机回环内部调用不要求 ANALYSIS_API_TOKEN，技能不应在发请求前自行拦截。"""
        class FakeAnalysisHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定方法名
                body = json.dumps({"products": [{"id": 1, "name": "Aiforce"}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):  # noqa: A002 - 基类签名固定
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAnalysisHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = dict(os.environ)
            env.pop("ANALYSIS_API_TOKEN", None)
            result = subprocess.run(
                [
                    NODE_BIN,
                    str(SKILL_DIR / "scripts" / "analysis-cli.mjs"),
                    "selftest",
                    "--base-url", f"http://127.0.0.1:{server.server_port}",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=str(BACKEND_DIR.parent),
                env=env,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"], payload)
        self.assertIsNone(payload["tokenSource"])
        self.assertEqual(payload["steps"][-1]["step"], "访问故障分析服务")
        self.assertTrue(payload["steps"][-1]["ok"])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_analysis_api_multipart_upload_accepts_port_rejected_by_node_fetch(self):
        """6000 端口的文件上传也必须保留 multipart Content-Type、boundary 和文件内容。"""
        class FakeUploadHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 约定方法名
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                content_type = self.headers.get("Content-Type", "")
                response = json.dumps({
                    "ok": True,
                    "multipart": content_type.startswith("multipart/form-data; boundary="),
                    "containsField": b'name="product_id"' in body,
                    "containsFile": b'name="file"; filename="sample.log"' in body and b"test-log" in body,
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, format, *args):  # noqa: A002 - 基类签名固定
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 6000), FakeUploadHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            analysis_url = (SKILL_DIR / "scripts" / "analysis.mjs").as_uri()
            driver = """
const analysis = await import("__ANALYSIS__");
const form = new FormData();
form.append("product_id", "1");
form.append("file", new Blob(["test-log"]), "sample.log");
const result = await analysis.apiRequest("POST", "/upload", {
  baseUrl: "http://127.0.0.1:6000",
  token: "test-token",
  formData: form,
});
console.log(JSON.stringify(result));
""".replace("__ANALYSIS__", analysis_url)
            output = self.run_node(driver)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        payload = json.loads(output.strip().splitlines()[-1])
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["multipart"])
        self.assertTrue(payload["containsField"])
        self.assertTrue(payload["containsFile"])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_scope_resolution_never_falls_back_to_module_branch(self):
        """故障分析只允许使用明确 Tag，工具里的 master 绑定不能作为兜底。"""
        analysis_url = (SKILL_DIR / "scripts" / "analysis.mjs").as_uri()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "scope-mapping.json"
            config_path.write_text(json.dumps({
                "versionRules": [{"name": "exact-tag", "pattern": "^.+$", "ref": "{version}"}],
                "products": {
                    "see": {
                        "modules": {
                            "see-task": {"repoUrl": "https://code.in.wezhuiyi.com/see/see-task.git"}
                        }
                    }
                },
            }, ensure_ascii=False), encoding="utf-8")
            driver = """
process.env.ANALYSIS_API_TOKEN = "test-token";
globalThis.fetch = async (url) => {
  const target = String(url);
  let payload = {};
  if (target.includes("/product/get")) payload = { products: [{ id: 1, name: "see" }] };
  else if (target.includes("/module/search")) payload = { modules: [{ module_id: 2, module_name: "see-task", branch: { branch_address: "https://code.in.wezhuiyi.com/see/see-task.git", tag_version: "master" } }] };
  else return { ok: false, status: 404, text: async () => JSON.stringify({ error: target }) };
  return { ok: true, status: 200, text: async () => JSON.stringify(payload) };
};
const { resolveScope } = await import("__ANALYSIS__");
const resolved = await resolveScope(
  { product: "see", module: "see-task", version: "3.2.1" },
  { configPath: process.argv[1] },
);
let withoutVersionError = "";
try {
  await resolveScope({ product: "see", module: "see-task" }, { configPath: process.argv[1] });
} catch (error) {
  withoutVersionError = String(error.message || error);
}
console.log(JSON.stringify({ resolved, withoutVersionError }));
""".replace("__ANALYSIS__", analysis_url)

            output = self.run_node(driver, str(config_path))

        payload = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(payload["resolved"]["tagVersion"], "3.2.1")
        self.assertIn("缺少代码仓库地址或 Tag", payload["withoutVersionError"])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_fault_scope_extracts_jira_product_and_comment_fallback(self):
        """产品从 Jira 字段读取，模块和版本可从固定评论块兜底。"""
        jira_url = (SKILL_DIR / "scripts" / "jira.mjs").as_uri()
        driver = """
const { normalizeIssue, extractFaultAnalysisScope } = await import("__JIRA__");
const issue = normalizeIssue({
  key: "KEY-2",
  id: "2",
  self: "https://jira.in.wezhuiyi.com/rest/api/2/issue/2",
  names: { customfield_10010: "问题发现产品" },
  fields: {
    summary: "任务回调失败",
    customfield_10010: { value: "see" },
    components: [],
    versions: [],
    comment: { total: 1, comments: [{
      id: "10",
      author: { displayName: "张三", name: "zhangsan" },
      created: "2026-09-20T09:40:00Z",
      body: "[FAULT_ANALYSIS]\\nmodule=see-task\\nversion=3.2.1"
    }] },
  },
});
console.log(JSON.stringify(extractFaultAnalysisScope(issue)));
""".replace("__JIRA__", jira_url)

        output = self.run_node(driver)
        payload = json.loads(output.strip().splitlines()[-1])

        self.assertEqual(payload["product"], "see")
        self.assertEqual(payload["module"], "see-task")
        self.assertEqual(payload["version"], "3.2.1")
        self.assertEqual(payload["sources"]["product"], "Jira:问题发现产品")
        self.assertEqual(payload["sources"]["module"], "评论:10")
        self.assertEqual(payload["missing"], [])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_fault_scope_accepts_module_tag_list_and_related_modules(self):
        """评论固定块支持模块:Tag 列表，首条为主模块，其余为关联模块。"""
        jira_url = (SKILL_DIR / "scripts" / "jira.mjs").as_uri()
        driver = """
const { normalizeIssue, extractFaultAnalysisScope } = await import("__JIRA__");
const issue = normalizeIssue({
  key: "KEY-4",
  id: "4",
  self: "https://jira.in.wezhuiyi.com/rest/api/2/issue/4",
  names: { customfield_10010: "问题发现产品" },
  fields: {
    summary: "任务回调失败",
    customfield_10010: { value: "see" },
    comment: { total: 1, comments: [{
      id: "11",
      author: { displayName: "张三", name: "zhangsan" },
      created: "2026-09-20T09:40:00Z",
      body: "[FAULT_ANALYSIS]\\nsee-task:v1.2.3\\nsee-management:v1.2.3"
    }] },
  },
});
console.log(JSON.stringify(extractFaultAnalysisScope(issue)));
""".replace("__JIRA__", jira_url)

        output = self.run_node(driver)
        payload = json.loads(output.strip().splitlines()[-1])

        self.assertEqual(payload["module"], "see-task")
        self.assertEqual(payload["version"], "v1.2.3")
        self.assertEqual(payload["relatedModules"], [{
            "module": "see-management",
            "version": "v1.2.3",
            "source": "评论:11",
        }])
        self.assertEqual(payload["missing"], [])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_fault_scope_reads_module_tag_pairs_from_description(self):
        """描述里每条「模块:Tag」都要识别：首条为主模块，其余整体作为关联模块保留。"""
        jira_url = (SKILL_DIR / "scripts" / "jira.mjs").as_uri()
        driver = """
const { normalizeIssue, extractFaultAnalysisScope } = await import("__JIRA__");
const issue = normalizeIssue({
  key: "KEY-13",
  id: "13",
  self: "https://jira.in.wezhuiyi.com/rest/api/2/issue/13",
  names: { customfield_10010: "问题发现产品" },
  fields: {
    summary: "aiforce 认证失败",
    description: "影响模块：aiforce-authentication\\n影响版本：v2.3_3-zhongyizaixian-20240425\\n影响模块：aiforce-model\\n影响版本：v2.3_3-zhongyizaixian-20240425",
    customfield_10010: { value: "Aiforce" },
    components: [{ name: "aiforce" }],
    comment: { total: 0, comments: [] },
  },
});
console.log(JSON.stringify(extractFaultAnalysisScope(issue)));
""".replace("__JIRA__", jira_url)

        output = self.run_node(driver)
        payload = json.loads(output.strip().splitlines()[-1])

        self.assertEqual(payload["product"], "Aiforce")
        self.assertEqual(payload["module"], "aiforce-authentication")
        self.assertEqual(payload["version"], "v2.3_3-zhongyizaixian-20240425")
        self.assertEqual(payload["sources"]["module"], "Jira:描述")
        self.assertEqual(payload["sources"]["version"], "Jira:描述")
        self.assertEqual(payload["relatedModules"], [{
            "module": "aiforce-model",
            "version": "v2.3_3-zhongyizaixian-20240425",
            "source": "Jira:描述",
        }])
        self.assertEqual(payload["missing"], [])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_fault_scope_keeps_cross_product_related_modules(self):
        """产品归属只填一个；描述里来自其它产品的模块及版本也要完整保留为关联模块。"""
        jira_url = (SKILL_DIR / "scripts" / "jira.mjs").as_uri()
        driver = """
const { normalizeIssue, extractFaultAnalysisScope } = await import("__JIRA__");
const issue = normalizeIssue({
  key: "KEY-17",
  id: "17",
  self: "https://jira.in.wezhuiyi.com/rest/api/2/issue/17",
  names: { customfield_10010: "产品归属" },
  fields: {
    summary: "aiforce 前台页面菜单打开空白",
    description: "影响模块：aiforce\\n影响版本：v5.1.1-zhongyizaixian-rc2\\n影响模块：bot-portal\\n影响版本：v0.3.0-ZYKJ20230177-rc122",
    customfield_10010: { value: "Aiforce" },
    components: [{ name: "aiforce" }],
    comment: { total: 0, comments: [] },
  },
});
console.log(JSON.stringify(extractFaultAnalysisScope(issue)));
""".replace("__JIRA__", jira_url)

        output = self.run_node(driver)
        payload = json.loads(output.strip().splitlines()[-1])

        self.assertEqual(payload["product"], "Aiforce")
        self.assertEqual(payload["sources"]["product"], "Jira:产品归属")
        self.assertEqual(payload["module"], "aiforce")
        self.assertEqual(payload["version"], "v5.1.1-zhongyizaixian-rc2")
        self.assertEqual(payload["relatedModules"], [{
            "module": "bot-portal",
            "version": "v0.3.0-ZYKJ20230177-rc122",
            "source": "Jira:描述",
        }])
        self.assertEqual(payload["missing"], [])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_fault_scope_product_echo_component_is_not_a_module(self):
        """Jira 组件与产品同名时只是产品回声，不能被当成已识别的模块。"""
        jira_url = (SKILL_DIR / "scripts" / "jira.mjs").as_uri()
        driver = """
const { normalizeIssue, extractFaultAnalysisScope } = await import("__JIRA__");
const issue = normalizeIssue({
  key: "KEY-14",
  id: "14",
  self: "https://jira.in.wezhuiyi.com/rest/api/2/issue/14",
  names: { customfield_10010: "问题发现产品" },
  fields: {
    summary: "aiforce 认证失败",
    customfield_10010: { value: "Aiforce" },
    components: [{ name: "aiforce" }],
    versions: [],
    comment: { total: 0, comments: [] },
  },
});
console.log(JSON.stringify(extractFaultAnalysisScope(issue)));
""".replace("__JIRA__", jira_url)

        output = self.run_node(driver)
        payload = json.loads(output.strip().splitlines()[-1])

        self.assertEqual(payload["product"], "Aiforce")
        self.assertEqual(payload["module"], "")
        self.assertEqual(payload["candidates"]["module"], [])
        self.assertNotIn("module", payload["sources"])
        self.assertEqual(payload["missing"], ["module", "version"])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_fault_scope_honours_explicit_module_named_like_product(self):
        """显式声明的模块按原样使用：与产品同名时交给 scope-mapping 的别名解析，不能悄悄丢弃。"""
        jira_url = (SKILL_DIR / "scripts" / "jira.mjs").as_uri()
        driver = """
const { normalizeIssue, extractFaultAnalysisScope } = await import("__JIRA__");
const issue = normalizeIssue({
  key: "KEY-16",
  id: "16",
  self: "https://jira.in.wezhuiyi.com/rest/api/2/issue/16",
  names: { customfield_10010: "问题发现产品" },
  fields: {
    summary: "aiforce 认证失败",
    customfield_10010: { value: "Aiforce" },
    components: [{ name: "aiforce" }],
    comment: { total: 1, comments: [{
      id: "22",
      author: { displayName: "李颖", name: "liying" },
      created: "2026-09-20T09:40:00Z",
      body: "[FAULT_ANALYSIS]\\naiforce:v5.1.1-zhongyizaixian-rc2"
    }] },
  },
});
console.log(JSON.stringify(extractFaultAnalysisScope(issue)));
""".replace("__JIRA__", jira_url)

        output = self.run_node(driver)
        payload = json.loads(output.strip().splitlines()[-1])

        self.assertEqual(payload["module"], "aiforce")
        self.assertEqual(payload["version"], "v5.1.1-zhongyizaixian-rc2")
        self.assertEqual(payload["sources"]["module"], "评论:22")
        self.assertEqual(payload["missing"], [])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_fault_scope_comment_block_outranks_description_pairs(self):
        """评论固定块是人工纠正入口，优先级高于描述里的模块:Tag，冲突项保留为关联模块。"""
        jira_url = (SKILL_DIR / "scripts" / "jira.mjs").as_uri()
        driver = """
const { normalizeIssue, extractFaultAnalysisScope } = await import("__JIRA__");
const issue = normalizeIssue({
  key: "KEY-15",
  id: "15",
  self: "https://jira.in.wezhuiyi.com/rest/api/2/issue/15",
  names: { customfield_10010: "问题发现产品" },
  fields: {
    summary: "aiforce 认证失败",
    description: "影响模块：aiforce-authentication\\n影响版本：v1.0.0",
    customfield_10010: { value: "Aiforce" },
    components: [{ name: "aiforce" }],
    comment: { total: 1, comments: [{
      id: "21",
      author: { displayName: "李颖", name: "liying" },
      created: "2026-09-20T09:40:00Z",
      body: "[FAULT_ANALYSIS]\\naiforce2:v5.1.1-zhongyizaixian-rc2"
    }] },
  },
});
console.log(JSON.stringify(extractFaultAnalysisScope(issue)));
""".replace("__JIRA__", jira_url)

        output = self.run_node(driver)
        payload = json.loads(output.strip().splitlines()[-1])

        self.assertEqual(payload["module"], "aiforce2")
        self.assertEqual(payload["version"], "v5.1.1-zhongyizaixian-rc2")
        self.assertEqual(payload["sources"]["module"], "评论:21")
        self.assertEqual(payload["relatedModules"], [{
            "module": "aiforce-authentication",
            "version": "v1.0.0",
            "source": "Jira:描述",
        }])
        self.assertEqual(payload["missing"], [])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_fault_scope_keeps_bare_module_for_backward_compatible_common_resolution(self):
        """兼容历史评论；新流程公共组件不写固定块，普通模块会在解析时因缺少 Tag 失败。"""
        jira_url = (SKILL_DIR / "scripts" / "jira.mjs").as_uri()
        driver = """
const { normalizeIssue, extractFaultAnalysisScope } = await import("__JIRA__");
const issue = normalizeIssue({
  key: "KEY-5",
  id: "5",
  self: "https://jira.in.wezhuiyi.com/rest/api/2/issue/5",
  names: { customfield_10010: "问题发现产品" },
  fields: {
    summary: "任务回调失败",
    customfield_10010: { value: "see" },
    comment: { total: 1, comments: [{
      id: "12",
      author: { displayName: "张三", name: "zhangsan" },
      created: "2026-09-20T09:40:00Z",
      body: "[FAULT_ANALYSIS]\\nsee-task:v1.2.3\\nbot-common"
    }] },
  },
});
console.log(JSON.stringify(extractFaultAnalysisScope(issue)));
""".replace("__JIRA__", jira_url)

        output = self.run_node(driver)
        payload = json.loads(output.strip().splitlines()[-1])

        self.assertEqual(payload["module"], "see-task")
        self.assertEqual(payload["version"], "v1.2.3")
        self.assertEqual(payload["relatedModules"], [{
            "module": "bot-common",
            "version": "",
            "source": "评论:12",
        }])
        self.assertEqual(payload["missing"], [])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_effect_bug_log_exemption_requires_exact_comment_phrase(self):
        """效果类缺陷只有命中固定语句，才能跳过日志/复现要求。"""
        jira_url = (SKILL_DIR / "scripts" / "jira.mjs").as_uri()
        driver = """
const { normalizeIssue, findLogRequirementExemption, renderIssueMarkdown } = await import("__JIRA__");
function issueWithComment(body) {
  return normalizeIssue({
    key: "KEY-6",
    id: "6",
    self: "https://jira.in.wezhuiyi.com/rest/api/2/issue/6",
    fields: {
      summary: "页面效果不符合预期",
      comment: { total: 1, comments: [{ id: "13", author: { displayName: "张三" }, created: "2026-09-20T09:40:00Z", body }] },
    },
  });
}
const exactIssue = issueWithComment("效果类缺陷。明确缺陷不需要额外提供日志信息");
const missingIssue = issueWithComment("效果类缺陷，不需要日志");
console.log(JSON.stringify({
  exact: findLogRequirementExemption(exactIssue),
  missing: findLogRequirementExemption(missingIssue),
  exactMarkdown: renderIssueMarkdown(exactIssue),
  missingMarkdown: renderIssueMarkdown(missingIssue),
}));
""".replace("__JIRA__", jira_url)

        output = self.run_node(driver)
        payload = json.loads(output.strip().splitlines()[-1])

        self.assertTrue(payload["exact"]["found"])
        self.assertEqual(payload["exact"]["matches"][0]["commentId"], "13")
        self.assertEqual(payload["exact"]["phrase"], "明确缺陷不需要额外提供日志信息")
        self.assertFalse(payload["missing"]["found"])
        self.assertIn("当前结果：已命中固定声明", payload["exactMarkdown"])
        self.assertIn("仅豁免日志与复现步骤", payload["exactMarkdown"])
        self.assertIn("其它 MUST 和全部 REDLINE 不豁免", payload["exactMarkdown"])
        self.assertIn("当前结果：未命中固定声明", payload["missingMarkdown"])
        self.assertIn("如本单确为效果类缺陷，请补充固定声明", payload["missingMarkdown"])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_scope_resolution_resolves_related_module_tag(self):
        """关联模块的仓库和 Tag 必须独立解析，并作为 relatedModules 传给分析接口。"""
        analysis_url = (SKILL_DIR / "scripts" / "analysis.mjs").as_uri()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "scope-mapping.json"
            config_path.write_text(json.dumps({
                "versionRules": [{"name": "exact-tag", "pattern": "^.+$", "ref": "{version}"}],
                "products": {
                    "see": {
                        "modules": {
                            "see-task": {"repoUrl": "https://code.in.wezhuiyi.com/see/see-task.git"},
                            "see-management": {"repoUrl": "https://code.in.wezhuiyi.com/see/see-management.git"},
                            "see-common": {"repoUrl": "https://code.in.wezhuiyi.com/see/see-common.git", "common": True}
                        }
                    }
                },
            }, ensure_ascii=False), encoding="utf-8")
            driver = """
process.env.ANALYSIS_API_TOKEN = "test-token";
globalThis.fetch = async (url) => {
  const target = String(url);
  let payload = {};
  if (target.includes("/product/get")) payload = { products: [{ id: 1, name: "see" }] };
  else if (target.includes("/module/search")) {
    const name = decodeURIComponent(new URL(target).searchParams.get("names"));
    payload = { modules: [{ module_id: name === "see-task" ? 2 : (name === "see-management" ? 3 : 4), module_name: name, branch: null }] };
  } else return { ok: false, status: 404, text: async () => JSON.stringify({ error: target }) };
  return { ok: true, status: 200, text: async () => JSON.stringify(payload) };
};
const { resolveScope } = await import("__ANALYSIS__");
const resolved = await resolveScope({
  product: "see",
  module: "see-task",
  version: "v1.2.3",
  related: [{ module: "see-management", version: "v1.2.3" }, { module: "see-common", version: "" }],
}, { configPath: process.argv[1] });
console.log(JSON.stringify(resolved));
""".replace("__ANALYSIS__", analysis_url)

            output = self.run_node(driver, str(config_path))

        payload = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(payload["tagVersion"], "v1.2.3")
        self.assertEqual(payload["relatedModules"], [{
            "moduleId": "3",
            "moduleName": "see-management",
            "branchAddress": "https://code.in.wezhuiyi.com/see/see-management.git",
            "tagVersion": "v1.2.3",
        }, {
            "moduleId": "4",
            "moduleName": "see-common",
            "branchAddress": "https://code.in.wezhuiyi.com/see/see-common.git",
            "tagVersion": "",
            "common": True,
        }])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_scope_resolution_finds_product_per_related_module(self):
        """产品归属只有一个，但关联模块可以属于别的产品；按模块反查自己的产品与仓库。"""
        analysis_url = (SKILL_DIR / "scripts" / "analysis.mjs").as_uri()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "scope-mapping.json"
            config_path.write_text(json.dumps({
                "versionRules": [{"name": "exact-tag", "pattern": "^.+$", "ref": "{version}"}],
                "products": {
                    "aiforce": {
                        "modules": {
                            "aiforce2": {
                                "repoUrl": "https://code.in.wezhuiyi.com/aiforce/aiforce2.git",
                                "aliases": ["aiforce"],
                            },
                            "aiforce-web": {
                                "repoUrl": "https://code.in.wezhuiyi.com/aiforce/aiforce2.git",
                            },
                        }
                    },
                    "bot": {
                        "modules": {
                            "bot-portal": {
                                "repoUrl": "https://code.in.wezhuiyi.com/bot/bot-portal.git",
                            },
                            "bot-common": {
                                "repoUrl": "https://code.in.wezhuiyi.com/bot/bot-common.git",
                                "common": True,
                            },
                        }
                    },
                },
            }, ensure_ascii=False), encoding="utf-8")
            driver = """
process.env.ANALYSIS_API_TOKEN = "test-token";
globalThis.fetch = async (url) => {
  const target = String(url);
  let payload = {};
  if (target.includes("/product/get")) {
    payload = { products: [{ id: 7, name: "aiforce" }, { id: 8, name: "bot" }] };
  } else if (target.includes("/module/search")) {
    const name = decodeURIComponent(new URL(target).searchParams.get("names"));
    const known = { "aiforce2": 70, "bot-portal": 72 };
    payload = { modules: known[name] ? [{ module_id: known[name], module_name: name, branch: null }] : [] };
  } else return { ok: false, status: 404, text: async () => JSON.stringify({ error: target }) };
  return { ok: true, status: 200, text: async () => JSON.stringify(payload) };
};
const { resolveScope } = await import("__ANALYSIS__");
const resolved = await resolveScope({
  product: "Aiforce",
  module: "aiforce",
  version: "v5.1.1-zhongyizaixian-rc2",
  related: [{ module: "bot-portal", version: "v0.3.0-ZYKJ20230177-rc122" }],
}, { configPath: process.argv[1] });
console.log(JSON.stringify(resolved));
""".replace("__ANALYSIS__", analysis_url)

            output = self.run_node(driver, str(config_path))

        payload = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(payload["productName"], "aiforce")
        self.assertEqual(payload["moduleName"], "aiforce2")
        self.assertEqual(payload["branchAddress"], "https://code.in.wezhuiyi.com/aiforce/aiforce2.git")
        self.assertEqual(payload["tagVersion"], "v5.1.1-zhongyizaixian-rc2")
        self.assertEqual(payload["relatedModules"], [{
            "moduleId": "72",
            "moduleName": "bot-portal",
            "branchAddress": "https://code.in.wezhuiyi.com/bot/bot-portal.git",
            "tagVersion": "v0.3.0-ZYKJ20230177-rc122",
        }])
        self.assertEqual(payload["commonModules"], [{
            "moduleId": "common:bot-common",
            "moduleName": "bot-common",
            "branchAddress": "https://code.in.wezhuiyi.com/bot/bot-common.git",
            "common": True,
            "aliases": [],
            "codePatterns": [],
        }])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_scope_resolution_promotes_product_owned_module_over_stale_comment(self):
        """旧固定块把 bot-portal 放在首位时，按产品归属纠偏为 aiforce2，并保留 bot-portal 关联。"""
        analysis_url = (SKILL_DIR / "scripts" / "analysis.mjs").as_uri()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "scope-mapping.json"
            config_path.write_text(json.dumps({
                "versionRules": [{"name": "exact-tag", "pattern": "^.+$", "ref": "{version}"}],
                "products": {
                    "aiforce": {
                        "modules": {
                            "aiforce2": {
                                "repoUrl": "https://code.in.wezhuiyi.com/aiforce/aiforce2.git",
                                "aliases": ["aiforce"],
                            },
                            "aiforce-web": {
                                "repoUrl": "https://code.in.wezhuiyi.com/aiforce/aiforce2.git",
                                "analysisModule": "aiforce2",
                            },
                        }
                    },
                    "bot": {
                        "modules": {
                            "bot-portal": {
                                "repoUrl": "https://code.in.wezhuiyi.com/bot/bot-portal.git",
                            },
                        }
                    },
                },
            }, ensure_ascii=False), encoding="utf-8")
            driver = """
process.env.ANALYSIS_API_TOKEN = "test-token";
globalThis.fetch = async (url) => {
  const target = String(url);
  let payload = {};
  if (target.includes("/product/get")) {
    payload = { products: [{ id: 7, name: "aiforce" }, { id: 8, name: "bot" }] };
  } else if (target.includes("/module/search")) {
    const name = decodeURIComponent(new URL(target).searchParams.get("names"));
    const known = { "aiforce2": 70, "bot-portal": 72 };
    payload = { modules: known[name] ? [{ module_id: known[name], module_name: name, branch: null }] : [] };
  } else return { ok: false, status: 404, text: async () => JSON.stringify({ error: target }) };
  return { ok: true, status: 200, text: async () => JSON.stringify(payload) };
};
const { resolveScope } = await import("__ANALYSIS__");
const resolved = await resolveScope({
  product: "Aiforce",
  module: "bot-portal",
  version: "v0.3.0-ZYKJ20230177-rc122",
  related: [{ module: "aiforce2", version: "v2.3.3-zhongyizaixian-20240425" }],
}, { configPath: process.argv[1] });
console.log(JSON.stringify(resolved));
""".replace("__ANALYSIS__", analysis_url)

            output = self.run_node(driver, str(config_path))

        payload = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(payload["moduleName"], "aiforce2")
        self.assertEqual(payload["branchAddress"], "https://code.in.wezhuiyi.com/aiforce/aiforce2.git")
        self.assertEqual(payload["tagVersion"], "v2.3.3-zhongyizaixian-20240425")
        self.assertEqual(payload["relatedModules"], [{
            "moduleId": "72",
            "moduleName": "bot-portal",
            "branchAddress": "https://code.in.wezhuiyi.com/bot/bot-portal.git",
            "tagVersion": "v0.3.0-ZYKJ20230177-rc122",
        }])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_scope_resolution_accepts_numeric_product_id_and_loads_common_candidates(self):
        """通过产品 ID 调用时，也必须解析出产品名并把公共组件候选提交给分析服务。"""
        analysis_url = (SKILL_DIR / "scripts" / "analysis.mjs").as_uri()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "scope-mapping.json"
            config_path.write_text(json.dumps({
                "versionRules": [{"name": "exact-tag", "pattern": "^.+$", "ref": "{version}"}],
                "products": {
                    "see": {
                        "modules": {
                            "see-task": {"repoUrl": "https://code.in.wezhuiyi.com/see/see-task.git"},
                            "see-common-util": {
                                "repoUrl": "https://code.in.wezhuiyi.com/see/see-common-util.git",
                                "common": True,
                            },
                        }
                    }
                },
            }, ensure_ascii=False), encoding="utf-8")
            driver = """
process.env.ANALYSIS_API_TOKEN = "test-token";
globalThis.fetch = async (url) => {
  const target = String(url);
  let payload = {};
  if (target.includes("/product/get")) payload = { products: [{ id: 1, name: "see" }] };
  else if (target.includes("/module/get")) payload = { modules: [{ module_id: 2, module_name: "see-task" }] };
  else return { ok: false, status: 404, text: async () => JSON.stringify({ error: target }) };
  return { ok: true, status: 200, text: async () => JSON.stringify(payload) };
};
const { resolveScope } = await import("__ANALYSIS__");
const resolved = await resolveScope({
  product: "1",
  module: "2",
  version: "v1.2.3",
}, { configPath: process.argv[1] });
console.log(JSON.stringify(resolved));
""".replace("__ANALYSIS__", analysis_url)

            output = self.run_node(driver, str(config_path))

        payload = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(payload["productId"], "1")
        self.assertEqual(payload["productName"], "see")
        self.assertEqual(payload["moduleName"], "see-task")
        self.assertEqual(payload["commonModules"], [{
            "moduleId": "common:see-common-util",
            "moduleName": "see-common-util",
            "branchAddress": "https://code.in.wezhuiyi.com/see/see-common-util.git",
            "common": True,
            "aliases": [],
            "codePatterns": [],
        }])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_fault_scope_rejects_ambiguous_jira_values(self):
        """Jira 模块或版本有多个候选时必须返回候选，不能猜第一个。"""
        jira_url = (SKILL_DIR / "scripts" / "jira.mjs").as_uri()
        driver = """
const { normalizeIssue, extractFaultAnalysisScope } = await import("__JIRA__");
const issue = normalizeIssue({
  key: "KEY-3",
  id: "3",
  self: "https://jira.in.wezhuiyi.com/rest/api/2/issue/3",
  names: { customfield_10010: "问题发现产品" },
  fields: {
    summary: "任务回调失败",
    customfield_10010: { value: "无" },
    components: [{ name: "see-task" }, { name: "see-common" }],
    versions: [{ name: "3.2.1" }, { name: "3.2.2" }],
    comment: { total: 0, comments: [] },
  },
});
console.log(JSON.stringify(extractFaultAnalysisScope(issue)));
""".replace("__JIRA__", jira_url)

        output = self.run_node(driver)
        payload = json.loads(output.strip().splitlines()[-1])

        self.assertEqual(payload["product"], "")
        self.assertEqual(payload["module"], "")
        self.assertEqual(payload["version"], "")
        self.assertEqual(payload["candidates"]["module"], ["see-task", "see-common"])
        self.assertEqual(payload["candidates"]["version"], ["3.2.1", "3.2.2"])
        self.assertEqual(payload["missing"], ["product", "module", "version"])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_fault_scope_extracts_module_version_from_description(self):
        """描述中的模块-版本号只作为版本值，不能把模块名一起交给 Tag 映射。"""
        jira_url = (SKILL_DIR / "scripts" / "jira.mjs").as_uri()
        driver = """
const { normalizeIssue, extractFaultAnalysisScope } = await import("__JIRA__");
const issue = normalizeIssue({
  key: "KEY-7",
  id: "7",
  self: "https://jira.in.wezhuiyi.com/rest/api/2/issue/7",
  names: { customfield_10010: "问题发现产品" },
  fields: {
    summary: "对话标签查询失败",
    description: "bot-3.2 模块发布后，对话标签查询失败。",
    customfield_10010: { value: "bot" },
    components: [{ name: "bot-server" }],
    comment: { total: 0, comments: [] },
  },
});
console.log(JSON.stringify(extractFaultAnalysisScope(issue)));
""".replace("__JIRA__", jira_url)

        output = self.run_node(driver)
        payload = json.loads(output.strip().splitlines()[-1])

        self.assertEqual(payload["version"], "3.2")
        self.assertEqual(payload["sources"]["version"], "Jira:描述")
        self.assertEqual(payload["missing"], [])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_fault_scope_extracts_module_version_from_summary(self):
        """概要中的模块-版本号同样属于工单内已有版本，可用于门禁通过。"""
        jira_url = (SKILL_DIR / "scripts" / "jira.mjs").as_uri()
        driver = """
const { normalizeIssue, extractFaultAnalysisScope } = await import("__JIRA__");
const issue = normalizeIssue({
  key: "KEY-11",
  id: "11",
  self: "https://jira.in.wezhuiyi.com/rest/api/2/issue/11",
  names: { customfield_10010: "问题发现产品" },
  fields: {
    summary: "bot-3.2 模块发布后，对话标签查询失败",
    customfield_10010: { value: "bot" },
    components: [{ name: "bot-server" }],
    comment: { total: 0, comments: [] },
  },
});
console.log(JSON.stringify(extractFaultAnalysisScope(issue)));
""".replace("__JIRA__", jira_url)

        output = self.run_node(driver)
        payload = json.loads(output.strip().splitlines()[-1])

        self.assertEqual(payload["version"], "3.2")
        self.assertEqual(payload["sources"]["version"], "Jira:概要")
        self.assertEqual(payload["missing"], [])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_fault_scope_does_not_use_fix_version_for_analysis(self):
        """只填写修复版本不能满足影响的版本要求，也不能作为自动分析版本来源。"""
        jira_url = (SKILL_DIR / "scripts" / "jira.mjs").as_uri()
        driver = """
const { normalizeIssue, extractFaultAnalysisScope } = await import("__JIRA__");
const issue = normalizeIssue({
  key: "KEY-8",
  id: "8",
  self: "https://jira.in.wezhuiyi.com/rest/api/2/issue/8",
  names: { customfield_10010: "问题发现产品" },
  fields: {
    summary: "对话标签查询失败",
    customfield_10010: { value: "bot" },
    components: [{ name: "bot-server" }],
    versions: [],
    fixVersions: [{ name: "3.2" }],
    comment: { total: 0, comments: [] },
  },
});
console.log(JSON.stringify(extractFaultAnalysisScope(issue)));
""".replace("__JIRA__", jira_url)

        output = self.run_node(driver)
        payload = json.loads(output.strip().splitlines()[-1])

        self.assertEqual(payload["version"], "")
        self.assertEqual(payload["missing"], ["version"])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_fault_scope_ignores_fix_version_text_for_analysis(self):
        """描述中明确写出的修复版本也不能代替影响的模块版本。"""
        jira_url = (SKILL_DIR / "scripts" / "jira.mjs").as_uri()
        driver = """
const { normalizeIssue, extractFaultAnalysisScope } = await import("__JIRA__");
const issue = normalizeIssue({
  key: "KEY-10",
  id: "10",
  self: "https://jira.in.wezhuiyi.com/rest/api/2/issue/10",
  names: { customfield_10010: "问题发现产品" },
  fields: {
    summary: "对话标签查询失败",
    description: "修复版本：bot-3.2",
    customfield_10010: { value: "bot" },
    components: [{ name: "bot-server" }],
    comment: { total: 0, comments: [] },
  },
});
console.log(JSON.stringify(extractFaultAnalysisScope(issue)));
""".replace("__JIRA__", jira_url)

        output = self.run_node(driver)
        payload = json.loads(output.strip().splitlines()[-1])

        self.assertEqual(payload["version"], "")
        self.assertEqual(payload["missing"], ["version"])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_fault_scope_accepts_agent_recognized_image_version_override(self):
        """图片中的模块版本经 Agent 识别后可作为调用方输入，无需额外交互确认。"""
        jira_url = (SKILL_DIR / "scripts" / "jira.mjs").as_uri()
        driver = """
const { normalizeIssue, extractFaultAnalysisScope } = await import("__JIRA__");
const issue = normalizeIssue({
  key: "KEY-9",
  id: "9",
  self: "https://jira.in.wezhuiyi.com/rest/api/2/issue/9",
  names: { customfield_10010: "问题发现产品" },
  fields: {
    summary: "对话标签查询失败",
    customfield_10010: { value: "bot" },
    components: [{ name: "bot-server" }],
    comment: { total: 0, comments: [] },
  },
});
console.log(JSON.stringify(extractFaultAnalysisScope(issue, { version: "3.2" })));
""".replace("__JIRA__", jira_url)

        output = self.run_node(driver)
        payload = json.loads(output.strip().splitlines()[-1])

        self.assertEqual(payload["version"], "3.2")
        self.assertEqual(payload["sources"]["version"], "调用方输入")
        self.assertEqual(payload["missing"], [])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_fault_scope_accepts_agent_recognized_image_module_overrides(self):
        """图片中的模块名、主 Tag 和关联模块应由 Agent 识别后写入调用方输入。"""
        jira_url = (SKILL_DIR / "scripts" / "jira.mjs").as_uri()
        driver = """
const { normalizeIssue, extractFaultAnalysisScope } = await import("__JIRA__");
const issue = normalizeIssue({
  key: "KEY-12",
  id: "12",
  self: "https://jira.in.wezhuiyi.com/rest/api/2/issue/12",
  names: { customfield_10010: "问题发现产品" },
  fields: {
    summary: "模型发布后对话分片异常",
    customfield_10010: { value: "bot" },
    components: [],
    comment: { total: 0, comments: [] },
  },
});
console.log(JSON.stringify(extractFaultAnalysisScope(issue, {
  module: "operation-task",
  version: "v2.3.73-update",
  relatedModules: [
    { module: "operation-admin", version: "v2.3.68" },
    { module: "dialogos", version: "v1.5.20" },
  ],
})));
""".replace("__JIRA__", jira_url)

        output = self.run_node(driver)
        payload = json.loads(output.strip().splitlines()[-1])

        self.assertEqual(payload["module"], "operation-task")
        self.assertEqual(payload["version"], "v2.3.73-update")
        self.assertEqual(payload["sources"]["module"], "调用方输入")
        self.assertEqual(payload["sources"]["version"], "调用方输入")
        self.assertEqual(payload["relatedModules"], [
            {"module": "operation-admin", "version": "v2.3.68", "source": "调用方输入:可辨认图片"},
            {"module": "dialogos", "version": "v1.5.20", "source": "调用方输入:可辨认图片"},
        ])
        self.assertEqual(payload["missing"], [])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_reject_transition_restores_original_assignee(self):
        """打回流转后必须恢复原经办人，不能让门禁账号留在经办人字段。"""
        jira_url = (SKILL_DIR / "scripts" / "jira.mjs").as_uri()
        driver = """
const { transitionIssue } = await import("__JIRA__");
const calls = [];
let issueReads = 0;
function response(data, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async text() { return data === null ? "" : JSON.stringify(data); },
  };
}
function issueRaw(assignee) {
  return {
    key: "KEY-20",
    id: "20",
    self: "https://jira.in.wezhuiyi.com/rest/api/2/issue/20",
    fields: {
      summary: "缺陷单信息不完整",
      status: { name: "待处理" },
      issuetype: { name: "缺陷" },
      assignee,
      reporter: { name: "reporter", displayName: "报告人" },
      components: [{ name: "see-task" }],
      subtasks: [{ key: "KEY-21", fields: { summary: "子任务" } }],
      comment: { total: 0, comments: [] },
    },
  };
}
globalThis.fetch = async (url, init = {}) => {
  const parsed = new URL(String(url));
  const body = init.body ? JSON.parse(String(init.body)) : null;
  calls.push({ method: init.method, path: parsed.pathname + parsed.search, body });
  if (init.method === "GET" && parsed.pathname.endsWith("/issue/KEY-20")) {
    issueReads += 1;
    return response(issueRaw(issueReads === 1
      ? { name: "zhangsan", displayName: "张三" }
      : { name: "gate-bot", displayName: "门禁机器人" }));
  }
  if (init.method === "GET" && parsed.pathname.endsWith("/issue/KEY-20/transitions")) {
    return response({ transitions: [{ id: "31", name: "打回", to: { name: "待补充" } }] });
  }
  if (init.method === "POST" && parsed.pathname.endsWith("/issue/KEY-20/transitions")) {
    return response(null, 204);
  }
  if (init.method === "PUT" && parsed.pathname.endsWith("/issue/KEY-20/assignee")) {
    return response(null, 204);
  }
  return response({ errorMessages: ["unexpected request: " + init.method + " " + parsed.pathname] }, 500);
};
const result = await transitionIssue("KEY-20", "打回");
console.log(JSON.stringify({ calls, result }));
""".replace("__JIRA__", jira_url)

        output = self.run_node(driver)
        payload = json.loads(output.strip().splitlines()[-1])
        assignee_call = next(item for item in payload["calls"] if item["method"] == "PUT" and item["path"].endswith("/assignee"))

        self.assertEqual(assignee_call["body"], {"name": "zhangsan"})
        self.assertTrue(payload["result"]["assigneeRestore"]["ok"])
        self.assertTrue(payload["result"]["assigneeRestore"]["restored"])
        self.assertEqual(payload["result"]["assigneeRestore"]["assignee"]["name"], "zhangsan")

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_set_issue_assignee_uses_login_name(self):
        """映射缺失转派时只能发送登录名，不能把中文展示名交给 Jira。"""
        jira_url = (SKILL_DIR / "scripts" / "jira.mjs").as_uri()
        driver = """
const { setIssueAssignee } = await import("__JIRA__");
const calls = [];
function response(data, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async text() { return data === null ? "" : JSON.stringify(data); },
  };
}
globalThis.fetch = async (url, init = {}) => {
  const parsed = new URL(String(url));
  const body = init.body ? JSON.parse(String(init.body)) : null;
  calls.push({ method: init.method, path: parsed.pathname, body });
  if (init.method === "PUT" && parsed.pathname.endsWith("/issue/KEY-30/assignee")) {
    return response(null, 204);
  }
  return response({ errorMessages: ["unexpected request"] }, 500);
};
const result = await setIssueAssignee("KEY-30", { name: "yinfuqian1193", displayName: "尹甫乾" });
console.log(JSON.stringify({ calls, result }));
""".replace("__JIRA__", jira_url)

        output = self.run_node(driver)
        payload = json.loads(output.strip().splitlines()[-1])

        self.assertEqual(payload["calls"], [{
            "method": "PUT",
            "path": "/rest/api/2/issue/KEY-30/assignee",
            "body": {"name": "yinfuqian1193"},
        }])
        self.assertEqual(payload["result"]["assignee"]["name"], "yinfuqian1193")

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_report_renders_styled_html_and_escapes_model_output(self):
        """报告必须包含关键章节，且对模型输出做 HTML 转义。"""
        report_url = (SKILL_DIR / "scripts" / "report.mjs").as_uri()
        driver = """
const { renderAnalysisHtml } = await import("__REPORT__");
const fs = await import("node:fs/promises");
const payload = JSON.parse(await fs.readFile(process.argv[1], "utf8"));
const html = renderAnalysisHtml(payload);
const checks = {
  escaped: html.includes("&lt;script&gt;") && !html.includes("<script>"),
  hasStyle: html.includes("<style>") ,
  hasConclusion: html.includes("回调超时"),
  hasEvidence: html.includes("分析输入") && html.includes("证据概览"),
  hasTask: html.includes("task-1"),
};
console.log(JSON.stringify(checks));
""".replace("__REPORT__", report_url)

        with tempfile.TemporaryDirectory() as temp_dir:
            payload_path = Path(temp_dir) / "payload.json"
            payload_path.write_text(json.dumps(self.SAMPLE_PAYLOAD, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [NODE_BIN, "-e", driver, str(payload_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=str(BACKEND_DIR.parent),
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        checks = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(checks["escaped"], "模型输出必须做 HTML 转义")
        self.assertTrue(checks["hasStyle"], "报告必须自带样式")
        self.assertTrue(checks["hasConclusion"])
        self.assertTrue(checks["hasEvidence"])
        self.assertTrue(checks["hasTask"])

    @unittest.skipIf(NODE_BIN is None, "需要 node 运行时才能执行技能脚本")
    def test_self_check_detection_requires_conclusion_and_evidence(self):
        """只有同时包含自查动作、结论与证据的评论才算有效自查结果。"""
        jira_url = (SKILL_DIR / "scripts" / "jira.mjs").as_uri()
        driver = """
const { findSelfCheckComments, normalizeIssue } = await import("__JIRA__");
const issue = normalizeIssue({
  key: "KEY-1",
  id: "1",
  self: "https://jira.in.wezhuiyi.com/rest/api/2/issue/1",
  fields: {
    summary: "工单回调报错",
    description: "实际：报错",
    comment: { total: 2, comments: [
      { id: "1", author: { displayName: "张三", name: "zhangsan" }, created: "2026-09-20T09:40:00Z",
        body: "【自查结果】结论：回调超时。证据：server.log 21:10 callback timeout，已复现。" },
      { id: "2", author: { displayName: "李四", name: "lisi" }, created: "2026-09-20T09:50:00Z", body: "已自查" },
    ] },
  },
});
console.log(JSON.stringify(findSelfCheckComments(issue)));
""".replace("__JIRA__", jira_url)

        output = self.run_node(driver)
        payload = json.loads(output.strip().splitlines()[-1])

        self.assertTrue(payload["found"])
        self.assertEqual([item["commentId"] for item in payload["matches"]], ["1"])
        self.assertIn("回调超时", payload["reason"])


if __name__ == "__main__":
    unittest.main()
