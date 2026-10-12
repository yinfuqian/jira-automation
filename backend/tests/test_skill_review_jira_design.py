"""review-jira-design 契约测试：技能随仓库分发、注册表可用、对 Jira 只读。"""
import importlib.util
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

PROJECT_ROOT = BACKEND_DIR.parent
SKILL_DIR = PROJECT_ROOT / "skills" / "review-jira-design"

NODE = shutil.which("node")
EVIDENCE_SCRIPT = SKILL_DIR / "scripts" / "capture_prototype_evidence.mjs"
REPORT_TEMPLATE = SKILL_DIR / "references" / "report-template.html"


class ReviewDesignSkillPackagingTests(unittest.TestCase):
    """技能需要能直接从仓库目录被发现与加载。"""

    def test_skill_files_are_shipped_with_the_repo(self):
        expected = [
            SKILL_DIR / "SKILL.md",
            SKILL_DIR / "agents" / "openai.yaml",
            SKILL_DIR / "references" / "review-checks.md",
            SKILL_DIR / "references" / "evidence-capture.md",
            REPORT_TEMPLATE,
            EVIDENCE_SCRIPT,
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

    def test_report_template_keeps_style_and_evidence_hooks(self):
        """SKILL.md 要求保留模板样式与证据展开交互，这里锁定这些结构。"""
        template = REPORT_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("<style>", template)
        self.assertIn("<script>", template)
        self.assertIn("{{需求名称}}", template)
        self.assertIn("evidence-inline", template)
        self.assertIn("evidence-toggle", template)


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
