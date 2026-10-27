"""jira-gate-2 契约测试：G2 技术方案准出、PDCA RD 双清单、故事类型门禁与状态回退。"""
import importlib.util
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
SKILLS_ROOT = PROJECT_ROOT / "skills"
GATE2_DIR = SKILLS_ROOT / "jira-gate-2"
GATE1_DIR = SKILLS_ROOT / "jira-gate-1"
NODE = shutil.which("node")


class Gate2DefinitionTests(unittest.TestCase):
    """技能随仓库分发，并且只处理「故事」类型。"""

    def test_required_files_are_shipped(self):
        expected = [
            GATE2_DIR / "SKILL.md",
            GATE2_DIR / "runtime.json",
            GATE2_DIR / "agents" / "openai.yaml",
            GATE2_DIR / "jira-config.json",
            GATE2_DIR / "references" / "checklist.md",
            GATE2_DIR / "references" / "jira-api.md",
            GATE2_DIR / "references" / "pdca-standard.md",
            GATE2_DIR / "scripts" / "jira.mjs",
            GATE2_DIR / "scripts" / "jira-cli.mjs",
            GATE2_DIR / "scripts" / "extract.py",
            GATE2_DIR / "scripts" / "workdir-cleanup.mjs",
        ]
        for path in expected:
            self.assertTrue(path.is_file(), f"缺少技能文件：{path}")

    def test_skill_is_registered(self):
        spec = importlib.util.spec_from_file_location(
            "skillrun_registry_gate2", BACKEND_DIR / "app" / "skillrun" / "registry.py"
        )
        registry = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = registry
        spec.loader.exec_module(registry)

        skill = registry.load_skill(GATE2_DIR, "jira-gate-2")

        self.assertEqual(skill.skill_id, "jira-gate-2")
        self.assertEqual(skill.name, "jira-gate-2")
        self.assertIn("G2", skill.description)
        self.assertEqual(skill.required_inputs, ("jira_url",))

    def test_skill_documents_type_gate_and_return_flow(self):
        text = (GATE2_DIR / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("只处理类型为「故事」", text)
        self.assertIn("precheck", text)
        self.assertIn("开发中", text)
        self.assertIn("开发设计中", text)
        self.assertIn("通过不流转", text)
        self.assertIn("--to 开发设计中", text)
        self.assertNotIn("--to 开发中", text)
        self.assertIn("(/)", text)
        self.assertIn("(x)", text)
        # G2 现在只额外产出 PDCA RD 报告，其它报告仍然不生成、不上传。
        self.assertIn("只生成 PDCA RD 角色报告", text)
        self.assertIn("不生成其他报告类文档", text)
        self.assertNotIn("review-jira-req", text)

    def test_type_gate_blocks_all_g2_and_pdca_actions(self):
        """类型门禁与 jira-gate-1 同口径：非「故事」时连 PDCA 判定与报告都不执行。"""
        text = (GATE2_DIR / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("类型门禁先于全部判定与写操作", text)
        self.assertIn("口径与 `jira-gate-1` 完全一致", text)
        self.assertIn("不执行 G2 Checklist 判定，也不执行 PDCA RD 检查", text)
        self.assertIn("不生成 PDCA 报告、不上传附件", text)
        self.assertIn("`conclusion` 写为 `不适用`", text)
        self.assertIn("前置条件：§1 类型预检必须已通过", text)
        self.assertIn("不执行 G2 与 PDCA 判定、不生成或上传 PDCA 报告", text)

    def test_supported_issue_types_match_gate1(self):
        """两个技能的受支持类型必须来自同一份定义，避免口径漂移。"""
        pattern = 'const SUPPORTED_ISSUE_TYPES = ["故事", "Story"];'

        self.assertIn(pattern, (GATE1_DIR / "scripts" / "jira.mjs").read_text(encoding="utf-8"))
        self.assertIn(pattern, (GATE2_DIR / "scripts" / "jira.mjs").read_text(encoding="utf-8"))

    def test_skill_runs_pdca_rd_check_together_with_g2_checklist(self):
        """两套判定必须同时执行，PDCA 只做 RD，且结论合并决定 G2 结论。"""
        text = (GATE2_DIR / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("### 5.1 G2 Checklist 逐项判定", text)
        self.assertIn("### 5.2 PDCA 开发交付物检查（RD）", text)
        self.assertIn("references/pdca-standard.md", text)
        # PDCA 必查项必须逐项落到评论模板。
        for index in range(1, 11):
            self.assertIn(f"D-{index:02d}", text)
        self.assertIn("只执行 RD 一类", text)
        self.assertIn("不得自动扩查 QA（测试方案/用例）或 UI（精修原型）", text)
        self.assertIn("唯一业务事实源", text)
        self.assertIn("不评价 PRD 自身质量", text)
        self.assertIn("责任方只写交付方（研发）", text)
        self.assertIn("不使用「有条件通过」", text)
        # 报告与上传规则。
        self.assertIn("_RD_{{YYYYMMDD}}_{{NN}}_PDCA.md", text)
        self.assertIn("attach-list", text)
        self.assertIn("CREATE_ATTACHMENTS", text)
        self.assertIn("检查未完成", text)
        self.assertIn("不生成空报告", text)

    def test_checklist_cross_references_pdca_standard(self):
        """原有 G2 Checklist 仍是门禁主判定，并显式声明与 PDCA 并行。"""
        text = (GATE2_DIR / "references" / "checklist.md").read_text(encoding="utf-8")

        self.assertIn("pdca-standard.md", text)
        self.assertIn("并行判定", text)

    def test_pdca_standard_is_vendored_verbatim(self):
        """PDCA 规范随技能分发，保留关键章节与结论口径。"""
        text = (GATE2_DIR / "references" / "pdca-standard.md").read_text(encoding="utf-8")

        self.assertIn("GENERIC_TEAM_DELIVERABLE_PDCA_SKILLS", text)
        self.assertIn("### 5.1 开发交付物", text)
        self.assertIn("### 5.2 测试交付物", text)
        self.assertIn("### 5.3 UI 交付物", text)
        self.assertIn("## 10. 禁止事项", text)
        self.assertIn("不要评价 PRD 或产品交付原型自身质量", text)

    def test_checklist_contains_all_g2_items(self):
        text = (GATE2_DIR / "references" / "checklist.md").read_text(encoding="utf-8")

        self.assertIn("# 技术方案准出 Checklist", text)
        for index in range(1, 11):
            self.assertIn(f"G2-M{index:02d}", text)
        for index in range(1, 12):
            self.assertIn(f"G2-C{index:02d}", text)
        self.assertIn("开发接管测试", text)
        self.assertIn("OpenSpec", text)

    def test_runtime_prompt_mentions_precheck_and_return_flow(self):
        payload = json.loads((GATE2_DIR / "runtime.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["required_inputs"], ["jira_url"])
        self.assertIn("precheck", payload["prompt_template"])
        self.assertIn("开发设计中", payload["prompt_template"])
        self.assertIn("结论「通过」不流转状态", payload["prompt_template"])
        self.assertNotIn("「通过」→ 流转到「开发中」", payload["prompt_template"])
        self.assertIn("pdca-standard.md", payload["prompt_template"])
        self.assertIn("PDCA RD", payload["prompt_template"])
        self.assertIn("D-01", payload["prompt_template"])
        self.assertIn("_PDCA.md", payload["prompt_template"])
        self.assertIn("role=RD", payload["prompt_template"])
        # 类型门禁必须覆盖 PDCA 判定与报告生成。
        self.assertIn("不执行 G2 与 PDCA 判定", payload["prompt_template"])
        self.assertIn("不生成 PDCA 报告", payload["prompt_template"])

    def test_gate_fields_use_g2_names(self):
        payload = json.loads((GATE2_DIR / "jira-config.json").read_text(encoding="utf-8"))
        source = (GATE2_DIR / "scripts" / "jira.mjs").read_text(encoding="utf-8")

        self.assertIn("G2准入结论", payload["gateResultField"])
        self.assertIn("G2准入结论", source)
        self.assertIn("G2 技术方案准出结论", source)

    def test_cleanup_targets_gate2_workdir(self):
        source = (GATE2_DIR / "scripts" / "workdir-cleanup.mjs").read_text(encoding="utf-8")

        self.assertIn('join("work", "jira-gate-2")', source)
        self.assertNotIn('join("work", "jira-gate")', source)


@unittest.skipUnless(NODE, "未安装 node，跳过类型预检脚本测试")
class IssueTypePrecheckTests(unittest.TestCase):
    """类型门禁是确定性的：只有「故事」/ Story 放行。"""

    def run_node(self, script: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [NODE, "-e", script],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )

    def test_gate1_supports_story_only(self):
        jira_url = (GATE1_DIR / "scripts" / "jira.mjs").as_uri()
        script = f"""
const m = await import("{jira_url}");
const cases = ["故事", "Story", "缺陷", "", null];
console.log(JSON.stringify(cases.map((item) => [item, m.isSupportedIssueType(item)])));
"""
        result = self.run_node(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = dict(json.loads(result.stdout))
        self.assertTrue(payload["故事"])
        self.assertTrue(payload["Story"])
        self.assertFalse(payload["缺陷"])
        self.assertFalse(payload[""])

    def test_gate2_supports_story_only(self):
        jira_url = (GATE2_DIR / "scripts" / "jira.mjs").as_uri()
        script = f"""
const m = await import("{jira_url}");
console.log(JSON.stringify([m.isSupportedIssueType("故事"), m.isSupportedIssueType("任务")]));
"""
        result = self.run_node(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [True, False])

    def test_cli_exposes_precheck(self):
        for skill_dir in (GATE1_DIR, GATE2_DIR):
            source = (skill_dir / "scripts" / "jira-cli.mjs").read_text(encoding="utf-8")
            self.assertIn('case "precheck"', source)
            self.assertIn("precheck <KEY|URL>", source)


if __name__ == "__main__":
    unittest.main()
