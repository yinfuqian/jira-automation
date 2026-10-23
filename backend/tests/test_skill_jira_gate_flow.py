"""技能流转契约测试：锁定 G1「通过不流转、退回补充→评审中」与故事类型门禁。"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = PROJECT_ROOT / "skills" / "jira-gate-1"
SKILL_PATH = SKILL_DIR / "SKILL.md"
JIRA_SCRIPT_PATH = SKILL_DIR / "scripts" / "jira.mjs"
CLI_PATH = SKILL_DIR / "scripts" / "jira-cli.mjs"
API_DOC_PATH = SKILL_DIR / "references" / "jira-api.md"
CLEANUP_SCRIPT_PATH = SKILL_DIR / "scripts" / "workdir-cleanup.mjs"
NODE = shutil.which("node")


class SkillJiraGateFlowTests(unittest.TestCase):
    def test_skill_documents_return_flow_and_type_gate(self):
        """SKILL.md 必须写明通过不流转、退回补充流转到「评审中」，并且只处理「故事」类型。"""
        text = SKILL_PATH.read_text(encoding="utf-8")

        self.assertIn("### 9. 交付顺序与「仅退回补充时流转并 @ 流转人」", text)
        self.assertIn("评审中", text)
        self.assertIn("--to 评审中", text)
        self.assertIn("通过不流转", text)
        self.assertIn("业务待决策事项", text)
        self.assertNotIn("决策卡", text)
        self.assertIn("只处理类型为「故事」", text)
        self.assertIn("precheck", text)
        self.assertNotIn("--to 开发设计中", text)
        self.assertNotIn("--to 需求设计", text)
        # 旧口径「一律不流转状态」必须已被替换，否则与本次需求冲突。
        self.assertNotIn("不流转状态、不指派经办人", text)

    def test_checklist_is_prd_admission_checklist(self):
        """判定标准必须换成 PRD 需求准入 Checklist，包含 8 项 MUST 与 6 组 CONDITIONAL。"""
        text = (SKILL_DIR / "references" / "checklist.md").read_text(encoding="utf-8")

        self.assertIn("# PRD 需求准入 Checklist", text)
        for must in ("事实源", "目标与范围", "核心链路", "业务规则", "状态与权限", "业务数据", "异常恢复", "验收标准"):
            self.assertIn(must, text)
        for group in ("UI 与字段", "角色、状态与高风险确认", "跨系统、异步与外部依赖", "迁移、历史兼容与存量对象", "LLM 业务质量、失败策略与副作用", "性能、安全、隐私与合规"):
            self.assertIn(group, text)
        self.assertIn("同一结果测试", text)

    def test_runtime_prompt_requires_precheck_return_flow_and_report(self):
        """服务端调用时必须先做类型预检，并完整执行 G1、三个评审技能与退回流转。"""
        payload = json.loads((SKILL_DIR / "runtime.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["required_inputs"], ["jira_url"])
        self.assertIn("precheck", payload["prompt_template"])
        self.assertIn("评审中", payload["prompt_template"])
        self.assertIn("结论「通过」不流转状态", payload["prompt_template"])
        self.assertIn("review-jira-req", payload["prompt_template"])
        self.assertIn("review-jira-design", payload["prompt_template"])
        self.assertIn("review-jira-qa", payload["prompt_template"])
        self.assertIn("调用顺序固定为 review-jira-req → review-jira-design → review-jira-qa", payload["prompt_template"])
        self.assertIn("build-report.mjs", payload["prompt_template"])
        self.assertIn("只执行阶段一", payload["prompt_template"])
        self.assertIn("ok=false", payload["prompt_template"])
        # QA 失败不能阻塞 G1：必须记录失败步骤并继续交付。
        self.assertIn("--non-blocking", payload["prompt_template"])
        self.assertIn("failedStep", payload["prompt_template"])
        self.assertIn("--run-id manual", payload["prompt_template"])
        self.assertIn("继续写 G1 评论", payload["prompt_template"])

    def test_skill_integrates_qa_gate_and_merges_conclusions(self):
        """G1 必须调用 review-jira-qa，并规定任一准出不通过即退回补充。"""
        text = SKILL_PATH.read_text(encoding="utf-8")

        self.assertIn("### 11.2 QA 需求准入门禁（review-jira-qa）", text)
        self.assertIn("/data/skills/review-jira-qa", text)
        self.assertIn("只执行阶段一", text)
        self.assertIn("G1 最终结论强制为**退回补充**", text)
        self.assertIn("运营性失败", text)
        self.assertIn("不得写成 QA 通过", text)
        self.assertIn('QA_WORKSPACE="$PWD/work/jira-gate/<KEY>/review-jira-qa"', text)
        self.assertIn("QA 清单附件", text)
        self.assertIn("QA 评论", text)
        # QA 运营性失败只记「未完成」，G1 后续交付与流转必须继续。
        self.assertIn("--non-blocking", text)
        self.assertIn("failedStep/error", text)
        self.assertIn("继续执行 G1 的评论", text)
        self.assertIn("不得中断 G1 后续交付", text)

    def test_skill_integrates_design_review_between_req_and_qa(self):
        """G1 必须在需求评审之后、QA 之前调用 review-jira-design，并上传设计报告。"""
        text = SKILL_PATH.read_text(encoding="utf-8")
        payload = json.loads((SKILL_DIR / "runtime.json").read_text(encoding="utf-8"))

        self.assertIn("### 11.1 原型设计准入报告（review-jira-design）", text)
        self.assertIn("/data/skills/review-jira-design", text)
        self.assertIn("build-report.mjs", text)
        self.assertIn("content.json", text)
        self.assertIn("<KEY>-原型设计准入报告.html", text)
        self.assertIn("审核未完成", text)
        self.assertIn("有条件通过", text)
        self.assertIn("原型设计准入", text)
        self.assertIn("设计报告附件", text)
        # 调用顺序必须是 req → design → qa，且都在 G1 评论之前。
        req_pos = text.index("### 11. 评审报告（review-jira-req）")
        design_pos = text.index("### 11.1 原型设计准入报告（review-jira-design）")
        qa_pos = text.index("### 11.2 QA 需求准入门禁（review-jira-qa）")
        self.assertLess(req_pos, design_pos)
        self.assertLess(design_pos, qa_pos)
        self.assertIn("review-jira-design", payload["prompt_template"])

    def test_delivery_order_runs_three_review_skills_before_comment(self):
        """三个评审技能必须在 G1 评论前完成，QA 评论/HTML 由 QA 技能自身交付。"""
        text = SKILL_PATH.read_text(encoding="utf-8")

        self.assertIn("review-jira-req` 五步流水线 → `review-jira-design` 原型设计准入 → `review-jira-qa` 阶段一", text)
        self.assertIn("QA 自己的评论和 HTML 上传由 `review-jira-qa` 在其阶段一内完成", text)
        self.assertIn("不要把 QA 的 HTML 再上传一次", text)
        self.assertIn("上传需求评审报告与设计准入报告", text)

    def test_comment_template_mentions_flow_owner(self):
        """不达标评论模板必须带 @ 流转人的「待处理人」行，且注明达标时省略。"""
        text = SKILL_PATH.read_text(encoding="utf-8")

        self.assertIn("待处理人：{{[~登录名]}}", text)
        self.assertIn("只在不达标时出现", text)

    def test_script_transition_matches_status_name_and_degrades(self):
        """流转脚本按目标状态名匹配，找不到流转时返回可读原因而不是抛错。"""
        source = JIRA_SCRIPT_PATH.read_text(encoding="utf-8")

        for name in ("listTransitions", "resolveFlowOwner", "transitionToStatus"):
            self.assertIn("export async function {0}".format(name), source)
        self.assertIn("already-in-target", source)
        self.assertIn('reason: "no-transition"', source)
        self.assertIn("flowOwnerField", source)
        # 回退顺序：优先配置字段，其次 assignee → reporter → creator。
        order = ['String(config.flowOwnerField || "assignee")', '"assignee"', '"reporter"', '"creator"']
        positions = [source.index(item) for item in order]
        self.assertEqual(positions, sorted(positions), "流转人回退顺序与预期不一致")

    def test_cli_exposes_flow_commands(self):
        """命令行入口必须提供 transitions / flow-owner / transition 三个子命令。"""
        source = CLI_PATH.read_text(encoding="utf-8")

        for token in ('case "transitions"', 'case "flow-owner"', 'case "transition"', "--to <目标状态>"):
            self.assertIn(token, source)

    def test_api_doc_covers_transition_permission_and_fallback(self):
        """接口文档需覆盖流转接口、TRANSITION_ISSUES 权限与找不到流转的降级说明。"""
        text = API_DOC_PATH.read_text(encoding="utf-8")

        self.assertIn("/rest/api/2/issue/{KEY}/transitions", text)
        self.assertIn("TRANSITION_ISSUES", text)
        self.assertIn("no-transition", text)
        self.assertIn("available", text)
        self.assertIn("--to 评审中", text)
        self.assertIn("通过不流转", text)

    def test_flow_owner_defaults_to_assignee(self):
        """流转人默认口径为经办人，文档与脚本必须一致地声明这一点。"""
        skill_text = SKILL_PATH.read_text(encoding="utf-8")
        doc_text = API_DOC_PATH.read_text(encoding="utf-8")

        self.assertIn("口径就是经办人", skill_text)
        self.assertIn("取经办人 `assignee`", doc_text)


class WorkdirCleanupDocTests(unittest.TestCase):
    """SKILL.md 必须写明收尾清理的范围、幂等性与「失败保留现场」。"""

    def test_skill_documents_cleanup_step(self):
        text = SKILL_PATH.read_text(encoding="utf-8")

        self.assertIn("### 12. 收尾：清理本次任务的工作区中间产物", text)
        self.assertIn("workdir-cleanup.mjs --key <KEY>", text)
        self.assertIn("`reports/` 不在清理范围内", text)
        self.assertIn("交付失败（异常中断）时不要调用本脚本", text)
        self.assertIn("按 §12 清理工作区中间产物", text)


@unittest.skipUnless(NODE, "未安装 node，跳过工作区清理测试")
class WorkdirCleanupTests(unittest.TestCase):
    """workdir-cleanup.mjs：只清本次任务的中间产物，路径写错时拒绝执行。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name) / "workspace"
        # 本次任务的中间产物：附件、解压产物与中间文件
        self.target = self.work / "work" / "jira-gate" / "CALL-1941"
        (self.target / "attachments" / "__unpacked").mkdir(parents=True)
        (self.target / "attachments" / "需求.rar").write_bytes(b"x" * 1024)
        (self.target / "attachments" / "__unpacked" / "prd.md").write_text("hi", encoding="utf-8")
        (self.target / "comment.txt").write_text("c", encoding="utf-8")
        # 别的单子的工作目录，必须不受影响
        self.other = self.work / "work" / "jira-gate" / "CALL-9999"
        self.other.mkdir(parents=True)
        (self.other / "keep.txt").write_text("keep", encoding="utf-8")
        # reports/ 是附件来源，不属于清理范围
        self.reports = self.work / "reports"
        self.reports.mkdir()
        (self.reports / "review-report-1.md").write_text("report", encoding="utf-8")

    def run_cleanup(self, *args, expect=0):
        """在临时工作目录里执行清理脚本，返回进程结果。"""
        result = subprocess.run(
            [NODE, str(CLEANUP_SCRIPT_PATH), *[str(item) for item in args]],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(self.work),
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, expect, result.stderr)
        return result

    def test_removes_only_this_task_workdir(self):
        """清理只删本次任务的目录，别的单子与 reports/ 不受影响。"""
        payload = json.loads(self.run_cleanup("--key", "CALL-1941").stdout)

        self.assertTrue(payload["removed"])
        self.assertEqual(payload["removedFileCount"], 3)
        self.assertFalse(self.target.exists())
        self.assertTrue((self.other / "keep.txt").is_file(), "别的单子的工作目录不能被删")
        self.assertTrue((self.reports / "review-report-1.md").is_file(), "reports/ 不在清理范围内")

    def test_is_idempotent_when_already_cleaned(self):
        """重复收尾不该报错：目录已不存在时返回 not-found。"""
        self.run_cleanup("--key", "CALL-1941")

        payload = json.loads(self.run_cleanup("--key", "CALL-1941").stdout)

        self.assertTrue(payload["ok"])
        self.assertFalse(payload["removed"])
        self.assertEqual(payload["reason"], "not-found")

    def test_dry_run_keeps_everything(self):
        """--dry-run 只统计文件数与字节数，不实际删除。"""
        payload = json.loads(self.run_cleanup("--key", "CALL-1941", "--dry-run").stdout)

        self.assertFalse(payload["removed"])
        self.assertEqual(payload["reason"], "dry-run")
        self.assertEqual(payload["removedFileCount"], 3)
        self.assertGreater(payload["removedBytes"], 0)
        self.assertTrue(self.target.exists())

    def test_refuses_path_traversal(self):
        """单号里带路径分隔符或 .. 时拒绝执行，绝不越出 work/jira-gate。"""
        for bad in ("../..", "..", "CALL-1941/../../..", "../../etc"):
            with self.subTest(key=bad):
                result = self.run_cleanup("--key", bad, expect=1)
                self.assertIn("unsafe-path", result.stderr)

        self.assertTrue((self.work / "work" / "jira-gate").is_dir(), "工作区不能被误删")

    def test_requires_key(self):
        """缺少 --key 时以退出码 1 报错，且不做任何删除。"""
        result = self.run_cleanup(expect=1)

        self.assertIn("key-missing", result.stderr)
        self.assertTrue(self.target.exists())


if __name__ == "__main__":
    unittest.main()
