"""jira-qa-2 / jira-qa-3 契约测试：锁定阶段拆分、接口入参不变与下载链路。"""
import json
import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

PROJECT_ROOT = BACKEND_DIR.parent
SKILLS_ROOT = PROJECT_ROOT / "skills"
STAGE2_DIR = SKILLS_ROOT / "jira-qa-2"
STAGE3_DIR = SKILLS_ROOT / "jira-qa-3"


class JiraQaSplitTests(unittest.TestCase):
    """校验 QA 阶段二/三已拆成两个技能，且不引入新的接口入参。"""

    def test_combined_skill_is_removed(self):
        """旧的合并技能目录应已删除，避免与拆分后的两个技能重名竞争。"""
        self.assertFalse((SKILLS_ROOT / "jira-qa").exists())

    def test_both_skills_ship_required_files(self):
        """两个技能都必须自带入口、脚本与安装包完整性依赖文件。"""
        for directory in (STAGE2_DIR, STAGE3_DIR):
            expected = [
                directory / "SKILL.md",
                directory / "README.md",
                directory / "VERSION",
                directory / "runtime.json",
                directory / "agents" / "openai.yaml",
                directory / "bin" / "prepare.mjs",
                directory / "bin" / "preflight.mjs",
                directory / "bin" / "selftest.mjs",
                directory / "bin" / "pack-deliverables.mjs",
                directory / "prompts" / "contract.md",
                directory / "templates" / "test-cases" / "test-case-template.xlsx",
            ]
            for path in expected:
                self.assertTrue(path.is_file(), f"缺少技能文件：{path}")

    def test_skills_are_registered_with_expected_ids(self):
        """front matter 名称必须等于目录名，/skill/run 才能按新 skill_id 调用。"""
        from app.skillrun.registry import load_skill

        stage2 = load_skill(STAGE2_DIR, "jira-qa-2")
        stage3 = load_skill(STAGE3_DIR, "jira-qa-3")

        self.assertEqual(stage2.name, "jira-qa-2")
        self.assertEqual(stage3.name, "jira-qa-3")
        self.assertTrue(stage2.description)
        self.assertTrue(stage3.description)

    def test_runtime_only_requires_jira_url(self):
        """不修改 /skill/run 入参：两个技能都只要求 jira_url，不得引入 stage。"""
        for directory in (STAGE2_DIR, STAGE3_DIR):
            payload = json.loads((directory / "runtime.json").read_text(encoding="utf-8"))
            prompt = payload["prompt_template"]

            self.assertEqual(payload["required_inputs"], ["jira_url"])
            self.assertNotIn("inputs.stage", prompt)
            self.assertNotIn("stage=design|cases|all", prompt)
            self.assertIn(".skill-result.json", prompt)
            self.assertIn("QA_RESULT_JSON", prompt)

    def test_stage2_skill_is_limited_to_stage2(self):
        """jira-qa-2 只能生成方案与 XMind，不得生成 Excel/CSV，也不得进入阶段三。"""
        text = (STAGE2_DIR / "SKILL.md").read_text(encoding="utf-8")
        prompt = json.loads((STAGE2_DIR / "runtime.json").read_text(encoding="utf-8"))["prompt_template"]

        self.assertIn("只执行阶段二", text)
        self.assertIn("/data/skills/jira-qa-2", text)
        self.assertIn("pack-deliverables.mjs", text)
        self.assertIn("mark-reviewed.mjs", text)
        self.assertIn("不生成 Excel、Xray CSV 或测试代码", text)
        self.assertIn("--stage 2", prompt)
        self.assertIn("禁止执行阶段一", prompt)
        self.assertIn("jira-qa-3", text)

    def test_stage3_skill_is_limited_to_stage3(self):
        """jira-qa-3 只消费复核后的基线，先从 Jira 回取，再生成 Excel/CSV，且不写 Jira。"""
        text = (STAGE3_DIR / "SKILL.md").read_text(encoding="utf-8")
        prompt = json.loads((STAGE3_DIR / "runtime.json").read_text(encoding="utf-8"))["prompt_template"]

        self.assertIn("只执行阶段三", text)
        self.assertIn("/data/skills/jira-qa-3", text)
        self.assertIn("fetch-artifacts.mjs", text)
        self.assertIn("build-cases.mjs", text)
        self.assertIn("validate-cases.mjs", text)
        self.assertIn("不写 Jira", text)
        self.assertIn("BLOCKED", text)
        self.assertIn("--stage 3", prompt)
        self.assertIn("fetch-artifacts.mjs", prompt)

    def test_prepare_rejects_other_stages(self):
        """脚本层也要硬约束：阶段二技能拒绝 stage 3，阶段三技能拒绝 stage 2。"""
        stage2 = (STAGE2_DIR / "bin" / "prepare.mjs").read_text(encoding="utf-8")
        stage3 = (STAGE3_DIR / "bin" / "prepare.mjs").read_text(encoding="utf-8")

        self.assertIn("jira-qa-2 只执行阶段二", stage2)
        self.assertIn("stage !== 'design'", stage2)
        self.assertIn("jira-qa-3 只执行阶段三", stage3)
        self.assertIn("stage !== 'cases'", stage3)

    def test_preflight_checks_own_command_file(self):
        """安装包完整性校验要指向各自命令入口，而不是已删除的 jira-qa.md。"""
        for directory, command in ((STAGE2_DIR, "command/jira-qa-2.md"), (STAGE3_DIR, "command/jira-qa-3.md")):
            source = (directory / "bin" / "preflight.mjs").read_text(encoding="utf-8")
            self.assertIn(command, source)
            self.assertNotIn("command/jira-qa.md", source)

    def test_pack_writes_zip_into_outputs_root(self):
        """两个技能都必须把 zip 落到 outputs 顶层，否则下载接口扫不到。"""
        for directory in (STAGE2_DIR, STAGE3_DIR):
            source = (directory / "bin" / "pack-deliverables.mjs").read_text(encoding="utf-8")
            self.assertIn("path.join('outputs')", source)
            self.assertIn("writeZip", source)

    def test_stage3_does_not_write_jira(self):
        """阶段三的技能脚本不得包含状态流转、门禁回写或附件上传。"""
        for name in ("fetch-artifacts.mjs", "pack-deliverables.mjs", "cases-result.mjs"):
            source = (STAGE3_DIR / "bin" / name).read_text(encoding="utf-8")
            self.assertNotIn("/transitions", source, f"{name} 不应调用状态流转接口")
            self.assertNotIn("setGateResult", source, f"{name} 不应写 G1 门禁字段")
            self.assertNotIn("uploadAttachment", source, f"{name} 不应上传附件")

    def test_stage2_result_keeps_stage_design(self):
        """阶段二结果仍使用 stage=design，统计口径与拆分前保持一致。"""
        source = (STAGE2_DIR / "bin" / "result.mjs").read_text(encoding="utf-8")

        self.assertIn("stage: 'design'", source)
        self.assertIn("designStatus", source)


if __name__ == "__main__":
    unittest.main()
