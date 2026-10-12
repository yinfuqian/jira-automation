"""review-jira-design 契约测试：技能随仓库分发、注册表可用、对 Jira 只读。"""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

PROJECT_ROOT = BACKEND_DIR.parent
SKILL_DIR = PROJECT_ROOT / "skills" / "review-jira-design"

NODE = shutil.which("node")
EVIDENCE_SCRIPT = SKILL_DIR / "scripts" / "capture_prototype_evidence.mjs"
BUILD_SCRIPT = SKILL_DIR / "scripts" / "build-report.mjs"
REPORT_TEMPLATE = SKILL_DIR / "references" / "report-template.html"
CONTENT_EXAMPLE = SKILL_DIR / "references" / "content-example.json"


class ReviewDesignSkillPackagingTests(unittest.TestCase):
    """技能需要能直接从仓库目录被发现与加载。"""

    def test_skill_files_are_shipped_with_the_repo(self):
        expected = [
            SKILL_DIR / "SKILL.md",
            SKILL_DIR / "agents" / "openai.yaml",
            SKILL_DIR / "references" / "review-checks.md",
            SKILL_DIR / "references" / "evidence-capture.md",
            CONTENT_EXAMPLE,
            REPORT_TEMPLATE,
            EVIDENCE_SCRIPT,
            BUILD_SCRIPT,
        ]
        for path in expected:
            self.assertTrue(path.is_file(), f"缺少技能文件：{path}")

    def test_skill_is_registered_under_its_directory_name(self):
        """注册表按目录名定位技能，front matter 的 name 必须与之一致。"""
        spec = importlib.util.spec_from_file_location(
            "skillrun_registry", BACKEND_DIR / "app" / "skillrun" / "registry.py"
        )
        registry = importlib.util.module_from_spec(spec)
        # dataclass 在解析注解时会回查 sys.modules，必须先注册再执行模块。
        sys.modules[spec.name] = registry
        spec.loader.exec_module(registry)

        skill = registry.load_skill(SKILL_DIR, "review-jira-design")

        self.assertEqual(skill.skill_id, "review-jira-design")
        self.assertEqual(skill.name, "review-jira-design")
        self.assertIn("原型", skill.description)

    def test_skill_documents_container_adaptation_and_read_only(self):
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("/data/skills/review-jira-design", text)
        self.assertIn("对 Jira 只读", text)
        self.assertIn("截图未验证", text)
        # 四种结论口径都要留在技能定义里。
        for conclusion in ("退回补齐", "有条件通过", "通过", "审核未完成"):
            self.assertIn(conclusion, text)

    def test_evidence_script_does_not_touch_jira_or_post_data(self):
        """截图脚本只操作本地文件与本地浏览器，不得访问 Jira 或外发数据。"""
        source = EVIDENCE_SCRIPT.read_text(encoding="utf-8")

        self.assertNotIn("jira", source.lower())
        self.assertNotIn("fetch(", source)
        self.assertNotIn("Authorization", source)

    def test_report_template_keeps_style_and_render_hooks(self):
        """SKILL.md 要求保留模板样式与浮层/折叠交互，这里锁定生成脚本依赖的结构。"""
        template = REPORT_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("<style>", template)
        self.assertIn("<script>", template)
        self.assertIn("{{需求名称}}", template)
        # 浮层与折叠区是 build-report.mjs 的渲染入口，模板被改写会直接破坏报告。
        self.assertIn("tipbox", template)
        self.assertIn("fold-body", template)

    def test_report_content_example_is_valid_json(self):
        """内容示例是撰写报告时的字段模板，必须是合法 JSON 且带必备区块。"""
        payload = json.loads(CONTENT_EXAMPLE.read_text(encoding="utf-8"))

        self.assertIn("结论", payload)
        self.assertIn("问题", payload)


@unittest.skipUnless(NODE, "未安装 node，跳过脚本语法检查")
class ReportBuildScriptTests(unittest.TestCase):
    """报告生成脚本的入口契约：语法可用、缺参数不静默成功、能按内容 JSON 合成 HTML。"""

    PRD = "## 3.3.4 BR-002：沙盒生产模式按钮禁用\n\n启用沙盒且处于生产模式时，四个批量按钮直接禁用且不得发起请求。\n"

    def run_script(self, args, cwd=None):
        """以统一参数跑生成脚本，返回 CompletedProcess。"""
        return subprocess.run(
            [NODE, str(BUILD_SCRIPT), *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=120,
            cwd=cwd,
        )

    def test_build_script_parses(self):
        result = subprocess.run(
            [NODE, "--check", str(BUILD_SCRIPT)],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_usage_exit_code_when_prd_missing(self):
        """缺 --prd 时按脚本约定以退出码 2 报用法。"""
        result = self.run_script([])

        self.assertEqual(result.returncode, 2)
        self.assertIn("用法", result.stderr)

    def test_dump_index_and_build_report(self):
        """内容示例能直接驱动脚本产出无残留占位符的自包含 HTML。"""
        with tempfile.TemporaryDirectory() as tmp:
            prd = Path(tmp) / "PRD_DELIVERY.md"
            prd.write_text(self.PRD, encoding="utf-8")

            index = self.run_script(["--prd", str(prd), "--dump-index"])
            self.assertEqual(index.returncode, 0, index.stderr)
            self.assertIn("BR-002", index.stdout)

            out = Path(tmp) / "report.html"
            built = self.run_script(["--prd", str(prd), "--content", str(CONTENT_EXAMPLE), "--out", str(out)])
            self.assertEqual(built.returncode, 0, built.stderr)
            self.assertTrue(out.is_file())

            html = out.read_text(encoding="utf-8")
            self.assertNotIn("{{", html)
            self.assertIn("<style>", html)
            self.assertIn("tipbox", html)


@unittest.skipUnless(NODE, "未安装 node，跳过脚本语法检查")
class EvidenceScriptSyntaxTests(unittest.TestCase):
    def test_evidence_script_parses(self):
        result = subprocess.run(
            [NODE, "--check", str(EVIDENCE_SCRIPT)],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_usage_exit_code_when_args_missing(self):
        """缺参数时按脚本约定以退出码 2 报用法，不静默成功。"""
        result = subprocess.run(
            [NODE, str(EVIDENCE_SCRIPT)],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("用法", result.stderr)


if __name__ == "__main__":
    unittest.main()
