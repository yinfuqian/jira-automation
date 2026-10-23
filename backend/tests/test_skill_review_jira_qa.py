"""review-jira-qa 契约测试：锁定技能目录、阶段一边界、运行配置与凭据安全。"""
import json
import os
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
SKILLS_ROOT = PROJECT_ROOT / "skills"
SKILL_DIR = SKILLS_ROOT / "review-jira-qa"
NODE = shutil.which("node")


class ReviewJiraQaVendoringTests(unittest.TestCase):
    """校验技能文件齐全、可被注册表加载，并保持与仓库目录一致。"""

    def test_skill_files_are_shipped_with_the_repo(self):
        """阶段一入口、提示词、模板、运行配置与代理描述都必须随仓库分发。"""
        expected = [
            SKILL_DIR / "SKILL.md",
            SKILL_DIR / "README.md",
            SKILL_DIR / "VERSION",
            SKILL_DIR / "runtime.json",
            SKILL_DIR / "agents" / "openai.yaml",
            SKILL_DIR / "prompts" / "contract.md",
            SKILL_DIR / "prompts" / "stage1-gate.md",
            SKILL_DIR / "bin" / "preflight.mjs",
            SKILL_DIR / "bin" / "jira-read.mjs",
            SKILL_DIR / "bin" / "jira-download.mjs",
            SKILL_DIR / "bin" / "materials.mjs",
            SKILL_DIR / "bin" / "coverage.mjs",
            SKILL_DIR / "bin" / "reuse.mjs",
            SKILL_DIR / "bin" / "render-gate.mjs",
            SKILL_DIR / "bin" / "validate-gate.mjs",
            SKILL_DIR / "bin" / "jira-upload.mjs",
            SKILL_DIR / "bin" / "jira-comment.mjs",
            SKILL_DIR / "bin" / "result.mjs",
            SKILL_DIR / "bin" / "selftest.mjs",
            SKILL_DIR / "bin" / "lib" / "fatal.mjs",
            SKILL_DIR / "templates" / "gate-html.html",
            SKILL_DIR / "templates" / "test-cases" / "test-case-template.xlsx",
            SKILL_DIR / "command" / "review-jira-qa.md",
            SKILL_DIR / "command" / "qa-risk-analysis.md",
        ]
        for path in expected:
            self.assertTrue(path.is_file(), f"缺少技能文件：{path}")

    def test_skill_is_registered_with_expected_name(self):
        """front matter 名称必须与目录名一致，否则 /skill/list 无法按 review-jira-qa 调用。"""
        from app.skillrun.registry import load_skill

        skill = load_skill(SKILL_DIR, "review-jira-qa")

        self.assertEqual(skill.skill_id, "review-jira-qa")
        self.assertEqual(skill.name, "review-jira-qa")
        self.assertTrue(skill.description)

    def test_skill_documents_container_path_and_stage1_boundary(self):
        """文档需给出容器路径，并明确 G1 调用时只执行阶段一。"""
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("/data/skills/review-jira-qa", text)
        self.assertIn("被 `jira-gate-1` 调用时只执行阶段一", text)
        self.assertIn("QA_RESULT_JSON", text)
        self.assertIn("不得自动进入阶段二或阶段三", text)
        self.assertIn("G1 最终结论必须为「退回补充」", text)
        self.assertIn("运营性失败", text)
        # 0.1.5 起统一以 status 表达异常与不适用；0.1.7 继续保持所有脚本 exit 0。
        self.assertIn("status=ERROR", text)
        self.assertIn("status=SKIPPED", text)
        self.assertIn("exit 0", text)
        self.assertIn("不能阻塞父流程继续执行", text)
        self.assertIn(".skill-result.json", text)
        self.assertNotIn("--non-blocking", text)
        self.assertNotIn("failedStep", text)
        self.assertEqual((SKILL_DIR / "VERSION").read_text(encoding="utf-8").strip(), "0.1.7")

    def test_runtime_defaults_to_stage1_only(self):
        """服务端直接调用该技能时也只能跑阶段一，并强制读取 Jira 链接。"""
        payload = json.loads((SKILL_DIR / "runtime.json").read_text(encoding="utf-8"))
        prompt = payload["prompt_template"]

        self.assertEqual(payload["required_inputs"], ["jira_url"])
        self.assertIn("只执行阶段一", prompt)
        self.assertIn("禁止执行阶段二或阶段三", prompt)
        self.assertIn("preflight", prompt)
        self.assertIn("validate-gate", prompt)
        self.assertIn("coverage-required", prompt)
        self.assertIn("coverage.mjs", prompt)
        self.assertIn("reuse.mjs", prompt)
        self.assertIn("REUSE=true", prompt)
        self.assertIn("--comments 10", prompt)
        self.assertIn("同类", prompt)
        self.assertIn("QA_RESULT_JSON", prompt)
        self.assertIn("不阻塞", prompt)
        self.assertIn("status=SKIPPED", prompt)
        self.assertIn("status=ERROR", prompt)
        self.assertIn("exit 0", prompt)
        self.assertIn("不适用", prompt)
        self.assertIn("未完成", prompt)
        self.assertIn(".skill-result.json", prompt)
        self.assertNotIn("--non-blocking", prompt)
        self.assertNotIn("failedStep", prompt)
        # runtime 模板必须是真实换行，不能把字面量 \n 塞给模型。
        self.assertIn("\n", prompt)
        self.assertNotIn("\\n", prompt)

    def test_scripts_read_credentials_from_environment_only(self):
        """凭据只能来自环境变量，不能通过命令行参数或令牌文件注入。"""
        env_source = (SKILL_DIR / "bin" / "lib" / "env.mjs").read_text(encoding="utf-8")

        self.assertIn("loadCredentials", env_source)
        self.assertIn("JIRA_PAT ?? env.JIRA_TOKEN", env_source)
        self.assertIn("不接受命令行参数", env_source)
        self.assertIn("isInside(resolved, skillDir())", env_source)
        self.assertIn("工作区不能位于技能安装目录内", env_source)

    def test_gate_spec_contract_is_strict(self):
        """阶段一只能输出通过/暂不通过，且不通过项必须有原文与关闭条件。"""
        html_source = (SKILL_DIR / "bin" / "lib" / "html.mjs").read_text(encoding="utf-8")

        self.assertIn("['通过', '暂不通过']", html_source)
        self.assertIn("['材料冲突', '材料未明确']", html_source)
        self.assertIn("quote", html_source)
        self.assertIn("closingConditions", html_source)
        self.assertIn("结论为「通过」时 failures 必须为空", html_source)

    def test_stage1_requires_full_ac_coverage_and_reuse(self):
        """0.1.7 必须全量覆盖 AC、记录复评来源，并支持同材料复用。"""
        prompt_text = (SKILL_DIR / "prompts" / "stage1-gate.md").read_text(encoding="utf-8")
        validate_source = (SKILL_DIR / "bin" / "validate-gate.mjs").read_text(encoding="utf-8")
        reuse_source = (SKILL_DIR / "bin" / "reuse.mjs").read_text(encoding="utf-8")
        coverage_source = (SKILL_DIR / "bin" / "coverage.mjs").read_text(encoding="utf-8")

        self.assertIn('"coverage"', prompt_text)
        self.assertIn("coverage-required.json", prompt_text)
        self.assertIn("同类扫描", prompt_text)
        self.assertIn("本版材料变更引入", prompt_text)
        self.assertIn("上一版漏检", prompt_text)
        self.assertIn("待确认来源", prompt_text)
        self.assertIn("coverage-required", validate_source)
        self.assertIn("coverage 缺少 AC", validate_source)
        self.assertIn("REUSE=", reuse_source)
        self.assertIn("required", coverage_source)

    def test_qa_skill_does_not_change_jira_status(self):
        """QA 阶段一只上传附件、写评论和产出结果，不负责 Jira 状态流转。"""
        for path in (SKILL_DIR / "bin").rglob("*.mjs"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("/transitions", source, f"{path} 不应调用状态流转接口")
            self.assertNotIn("setGateResult", source, f"{path} 不应写 G1 门禁字段")


@unittest.skipUnless(NODE, "未安装 node，跳过异常收口测试")
class FatalResultTests(unittest.TestCase):
    """脚本异常必须写 status=ERROR 结果，并以 0 退出把控制权交还父技能。"""

    def test_preflight_failure_returns_zero_and_writes_error_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["QA_WORKSPACE"] = tmp
            for key in ("JIRA_BASE_URL", "JIRA_PAT", "JIRA_TOKEN"):
                env.pop(key, None)
            result = subprocess.run(
                [
                    NODE,
                    str(SKILL_DIR / "bin" / "preflight.mjs"),
                    "--key",
                    "QA-101",
                    "--stage",
                    "1",
                ],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                cwd=tmp,
                env=env,
                check=False,
                timeout=60,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("QA_RESULT_JSON", result.stdout)
            result_files = list((Path(tmp) / ".qa" / "runs").glob("*/result.json"))
            self.assertEqual(len(result_files), 1)
            payload = json.loads(result_files[0].read_text(encoding="utf-8"))
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["status"], "ERROR")
            self.assertTrue(payload["applicable"])
            self.assertTrue(payload["errorCode"])
            self.assertTrue(payload["error"])


if __name__ == "__main__":
    unittest.main()
