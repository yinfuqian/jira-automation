"""jira-gate-1-mock 契约测试：故事类型门禁、只读边界、压缩包交付与打包脚本行为。"""
import json
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

PROJECT_ROOT = BACKEND_DIR.parent
SKILLS_ROOT = PROJECT_ROOT / "skills"
MOCK_SKILL_DIR = SKILLS_ROOT / "jira-gate-1-mock"
GATE_SKILL_DIR = SKILLS_ROOT / "jira-gate-1"

NODE = shutil.which("node")
PACK_SCRIPT = MOCK_SKILL_DIR / "scripts" / "pack-deliverables.mjs"

# 预演版脚本里不允许出现写 Jira 的方法；这些名字一旦出现说明只读边界被破坏。
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
WRITE_FUNCTIONS = ("addComment", "updateComment", "uploadAttachment", "setGateResult", "transitionToStatus")


class MockSkillPackagingTests(unittest.TestCase):
    """技能随仓库分发：入口、判定清单与打包脚本都要在。"""

    def test_skill_files_are_shipped_with_the_repo(self):
        expected = [
            MOCK_SKILL_DIR / "SKILL.md",
            MOCK_SKILL_DIR / "runtime.json",
            MOCK_SKILL_DIR / "agents" / "openai.yaml",
            MOCK_SKILL_DIR / "references" / "checklist.md",
            MOCK_SKILL_DIR / "references" / "jira-api.md",
            MOCK_SKILL_DIR / "scripts" / "jira.mjs",
            MOCK_SKILL_DIR / "scripts" / "jira-cli.mjs",
            MOCK_SKILL_DIR / "scripts" / "extract.py",
            MOCK_SKILL_DIR / "scripts" / "report-conclusion.mjs",
            MOCK_SKILL_DIR / "scripts" / "workdir-cleanup.mjs",
            PACK_SCRIPT,
        ]
        for path in expected:
            self.assertTrue(path.is_file(), f"缺少技能文件：{path}")

    def test_skill_is_registered_as_usable_skill(self):
        """技能目录需满足注册表约定：front matter 的 name 等于目录名。"""
        # 直接从文件加载 registry，避免导入 app 包（它会拉起 Flask，测试环境未必装）。
        spec = importlib.util.spec_from_file_location(
            "skillrun_registry", BACKEND_DIR / "app" / "skillrun" / "registry.py"
        )
        registry = importlib.util.module_from_spec(spec)
        # dataclass 在解析注解时会回查 sys.modules，必须先注册再执行模块。
        sys.modules[spec.name] = registry
        spec.loader.exec_module(registry)

        skill = registry.load_skill(MOCK_SKILL_DIR, "jira-gate-1-mock")

        self.assertEqual(skill.skill_id, "jira-gate-1-mock")
        self.assertEqual(skill.name, "jira-gate-1-mock")
        self.assertIn("只读", skill.description)

    def test_checklist_is_identical_to_gate_skill(self):
        """判定标准必须与 jira-gate-1 完全一致，避免两个技能口径漂移。"""
        self.assertEqual(
            (MOCK_SKILL_DIR / "references" / "checklist.md").read_bytes(),
            (GATE_SKILL_DIR / "references" / "checklist.md").read_bytes(),
        )


class MockSkillReadOnlyTests(unittest.TestCase):
    """只读边界由脚本层保证，不依赖调用方自觉。"""

    def test_request_is_get_only(self):
        source = (MOCK_SKILL_DIR / "scripts" / "jira.mjs").read_text(encoding="utf-8")

        self.assertIn('method: "GET"', source)
        for method in WRITE_METHODS:
            self.assertNotIn(f'"{method}"', source, f"jira.mjs 不应出现写方法 {method}")
        for name in WRITE_FUNCTIONS:
            self.assertNotIn(f"export async function {name}", source, f"jira.mjs 不应导出写函数 {name}")

    def test_cli_exposes_only_read_commands(self):
        source = (MOCK_SKILL_DIR / "scripts" / "jira-cli.mjs").read_text(encoding="utf-8")

        self.assertIn('case "precheck"', source)
        self.assertIn('case "get"', source)
        self.assertIn('case "attach-list"', source)
        for command in ("comment", "comment-update", "gate-set", "transition"):
            self.assertNotIn(f'case "{command}"', source, f"jira-cli.mjs 不应提供写命令 {command}")
        self.assertNotIn('case "attach"', source)
        for method in WRITE_METHODS:
            self.assertNotIn(f'"{method}"', source)

    def test_issue_type_gate_is_story_only(self):
        """类型门禁只放行「故事」/ Story，且脚本仍不包含任何写方法。"""
        source = (MOCK_SKILL_DIR / "scripts" / "jira.mjs").read_text(encoding="utf-8")

        self.assertIn('const SUPPORTED_ISSUE_TYPES = ["故事", "Story"]', source)
        self.assertIn("export function isSupportedIssueType", source)
        self.assertIn("export async function precheckIssueType", source)

        if NODE:
            jira_url = (MOCK_SKILL_DIR / "scripts" / "jira.mjs").as_uri()
            script = f"""
const m = await import("{jira_url}");
console.log(JSON.stringify([m.isSupportedIssueType("故事"), m.isSupportedIssueType("Story"), m.isSupportedIssueType("缺陷")]));
"""
            result = subprocess.run(
                [NODE, "-e", script],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), [True, True, False])

    def test_runtime_prompt_requires_precheck_and_packaging(self):
        """服务端调用时必须先做类型预检，并把结果打进压缩包。"""
        payload = json.loads((MOCK_SKILL_DIR / "runtime.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["required_inputs"], ["jira_url"])
        self.assertIn("precheck", payload["prompt_template"])
        self.assertIn("review-jira-req", payload["prompt_template"])
        self.assertIn("review-jira-design", payload["prompt_template"])
        self.assertIn("pack-deliverables.mjs", payload["prompt_template"])

    def test_skill_documents_read_only_and_package_delivery(self):
        """SKILL.md 要把「故事类型门禁」「PRD 清单」「不写 Jira」「压缩包交付」写清楚。"""
        text = (MOCK_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("只处理类型为「故事」", text)
        self.assertIn("precheck <KEY|URL>", text)
        self.assertIn("PRD 需求准入 Checklist", text)
        self.assertIn("### 5. 逐项判定", text)
        self.assertIn("5.1 事实源", text)
        self.assertIn("6.6.2 性能/安全/隐私-敏感数据", text)
        self.assertIn("开发设计中", text)
        self.assertIn("需求设计", text)
        self.assertIn("评审中", text)
        self.assertIn("业务待决策事项", text)
        self.assertIn("检查项判定明细", text)
        self.assertNotIn("决策卡", text)
        self.assertNotIn("通过→开发设计中", text)
        self.assertNotIn("退回补充→需求设计", text)
        self.assertIn("### 12. 打包交付", text)
        self.assertIn("### 11. 生成原型设计准入报告（review-jira-design）", text)
        self.assertIn("### 13. 收尾：清理本次任务的工作区中间产物", text)
        self.assertIn("pack-deliverables.mjs", text)
        self.assertIn("outputs/<KEY>-G1预检包-", text)
        self.assertIn("<KEY>-G1预检评论.txt", text)
        self.assertIn("<KEY>-需求评审报告-宋立志.md", text)
        self.assertIn("<KEY>-原型设计准入报告.html", text)
        self.assertIn("**对 Jira 严格只读**", text)
        self.assertNotIn("难度：", text)
        self.assertNotIn("G1需求难度", text)
        # 旧的写 Jira 口径不得残留。
        self.assertNotIn("jira-cli.mjs comment ", text)
        self.assertNotIn("jira-cli.mjs attach <", text)
        self.assertNotIn("jira-cli.mjs transition ", text)
        self.assertNotIn("上传评审报告附件", text)

    def test_skill_requires_two_review_skills(self):
        """预演流程必须依次调用需求评审与原型设计审核。"""
        text = (MOCK_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("review-jira-req", text)
        self.assertIn("review-jira-design", text)
        self.assertLess(text.index("### 10. 生成需求质量评审报告"), text.index("### 11. 生成原型设计准入报告"))


@unittest.skipUnless(NODE, "未安装 node，跳过打包脚本测试")
class PackDeliverablesTests(unittest.TestCase):
    """pack-deliverables.mjs：评论草稿 + 评审报告 → 压缩包。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.comment = self.work / "comment.txt"
        self.comment.write_text(
            "h3. G1 需求准入结论（预演）：退回补充（流转至评审中）\n"
            "\n"
            "* 结论：退回补充 | 依据：PRD 需求准入 Checklist\n"
            "* 建议处理人：[~liu.huan]｜建议流转：评审中（预演未执行）\n"
            "\n"
            "h4. MUST（8 项）\n"
            "\n"
            "# (x) *5.2 目标与范围*（MUST）：未写业务价值 → 补影响范围；证据：PRD §1\n"
            "\n"
            "* 报告附件：CALL-1941-需求评审报告-宋立志.md（已在压缩包内）｜评审结论：需修改\n",
            encoding="utf-8",
        )
        self.report = self.work / "report.md"
        self.report.write_text("# 评审报告\n\n### CALL-1941 需求\n\n- 结论：**需修改**\n", encoding="utf-8")

    def run_pack(self, *args, expect=0):
        """在临时工作目录里执行打包脚本，返回进程结果。"""
        result = subprocess.run(
            [NODE, str(PACK_SCRIPT), *[str(item) for item in args]],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(self.work),
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, expect, result.stderr)
        return result

    def test_packs_comment_and_report_into_zip(self):
        """压缩包内含评论草稿与规范命名的评审报告，默认落在 outputs/。"""
        payload = json.loads(self.run_pack("--key", "call-1941", "--comment", "comment.txt", "--report", "report.md").stdout)

        self.assertTrue(payload["ok"])
        zip_path = Path(payload["zip"])
        self.assertTrue(zip_path.is_file())
        self.assertEqual(zip_path.parent.name, "outputs")
        self.assertGreater(payload["zipBytes"], 0)
        self.assertEqual(payload["warnings"], [])

        with zipfile.ZipFile(zip_path) as archived:
            names = archived.namelist()
            self.assertIn("CALL-1941-G1预检评论.txt", names)
            self.assertIn("CALL-1941-需求评审报告-宋立志.md", names)
            self.assertIn("需修改", archived.read("CALL-1941-需求评审报告-宋立志.md").decode("utf-8"))
            self.assertIn("(x)", archived.read("CALL-1941-G1预检评论.txt").decode("utf-8"))

    def test_zip_marks_filenames_as_utf8(self):
        """中文文件名必须置 UTF-8 标志位，否则 Windows 解压会乱码。"""
        payload = json.loads(self.run_pack("--key", "CALL-1941", "--comment", "comment.txt", "--report", "report.md").stdout)
        raw = Path(payload["zip"]).read_bytes()

        self.assertEqual(raw[0:4], b"PK\x03\x04")
        flags = int.from_bytes(raw[6:8], "little")
        self.assertTrue(flags & 0x0800, "local header 缺少 UTF-8 文件名标志位")

    def test_extra_files_and_custom_output_dir(self):
        """--extra 追加附加文件，--out 改写输出目录。"""
        extra = self.work / "issue.md"
        extra.write_text("取数摘要", encoding="utf-8")

        payload = json.loads(
            self.run_pack(
                "--key", "CALL-1941",
                "--comment", "comment.txt",
                "--report", "report.md",
                "--extra", "issue.md",
                "--out", "custom-out",
            ).stdout
        )

        self.assertEqual(Path(payload["zip"]).parent.name, "custom-out")
        with zipfile.ZipFile(payload["zip"]) as archived:
            self.assertIn("issue.md", archived.namelist())

    def test_warns_on_markdown_syntax_in_comment(self):
        """评论草稿残留 Markdown 粗体时给出 warning，但不阻断打包。"""
        self.comment.write_text(
            "h3. G1 需求准入结论（预演）：通过\n\n# (/) *5.1 事实源*（MUST）\n\n**粗体**\n\n* 报告附件：x｜评审结论：合格\n",
            encoding="utf-8",
        )

        payload = json.loads(self.run_pack("--key", "CALL-1", "--comment", "comment.txt", "--report", "report.md").stdout)

        self.assertTrue(payload["ok"])
        self.assertTrue(any("Markdown" in item for item in payload["warnings"]), payload["warnings"])

    def test_packs_design_report_with_fixed_name(self):
        """--design 指到的 HTML 进包后固定命名为 <KEY>-原型设计准入报告.html。"""
        design = self.work / "design" / "capture-draft.html"
        design.parent.mkdir(parents=True, exist_ok=True)
        design.write_text(
            "<!DOCTYPE html><html><body><h1>原型设计准入审核</h1><p>结论：通过</p></body></html>",
            encoding="utf-8",
        )

        payload = json.loads(
            self.run_pack(
                "--key", "CALL-1941",
                "--comment", "comment.txt",
                "--report", "report.md",
                "--design", "design/capture-draft.html",
            ).stdout
        )

        self.assertEqual(payload["warnings"], [])
        names = [item["name"] for item in payload["entries"]]
        self.assertEqual(
            names,
            [
                "CALL-1941-G1预检评论.txt",
                "CALL-1941-需求评审报告-宋立志.md",
                "CALL-1941-原型设计准入报告.html",
            ],
        )
        with zipfile.ZipFile(payload["zip"]) as archived:
            self.assertIn("结论：通过", archived.read("CALL-1941-原型设计准入报告.html").decode("utf-8"))

    def test_warns_on_unreplaced_placeholders_in_design_report(self):
        """设计报告残留 {{...}} 占位符或缺结论时给 warning，但不阻断打包。"""
        design = self.work / "design.html"
        design.write_text("<html><body><p>{{需求名称}} · {{PRD版本}}</p></body></html>", encoding="utf-8")

        payload = json.loads(
            self.run_pack(
                "--key", "CALL-1",
                "--comment", "comment.txt",
                "--report", "report.md",
                "--design", "design.html",
            ).stdout
        )

        self.assertTrue(payload["ok"])
        self.assertTrue(any("占位符" in item for item in payload["warnings"]), payload["warnings"])
        self.assertTrue(any("结论" in item for item in payload["warnings"]), payload["warnings"])

    def test_fails_when_design_file_missing(self):
        """--design 指到不存在的文件时以退出码 1 报错。"""
        result = self.run_pack(
            "--key", "CALL-1",
            "--comment", "comment.txt",
            "--report", "report.md",
            "--design", "nope.html",
            expect=1,
        )

        self.assertIn("file-not-found", result.stderr)

    def test_fails_when_comment_file_missing(self):
        """评论草稿缺失时以退出码 1 报错，不产出半成品。"""
        result = self.run_pack("--key", "CALL-1", "--comment", "nope.txt", "--report", "report.md", expect=1)

        self.assertIn("file-not-found", result.stderr)
        self.assertFalse((self.work / "outputs").exists(), "失败时不应写出压缩包")

    def test_refuses_illegal_key(self):
        """单号里带路径分隔符或 .. 时拒绝执行。"""
        for bad in ("../../etc/passwd", "CALL-1941/../..", "not-a-key"):
            with self.subTest(key=bad):
                result = self.run_pack("--key", bad, "--comment", "comment.txt", "--report", "report.md", expect=1)
                self.assertIn("key-invalid", result.stderr)


if __name__ == "__main__":
    unittest.main()
