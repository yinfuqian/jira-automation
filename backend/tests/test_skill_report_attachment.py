"""评审报告附件契约测试：锁定 review-jira-req 的引入方式与附件命名规则。"""
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
GATE_SKILL_DIR = SKILLS_ROOT / "jira-gate-1"
REVIEW_SKILL_DIR = SKILLS_ROOT / "review-jira-req"


class ReviewSkillVendoringTests(unittest.TestCase):
    def test_review_skill_files_are_shipped_with_the_repo(self):
        """新技能需随仓库分发：入口、提示词与零依赖脚本都要在。"""
        expected = [
            REVIEW_SKILL_DIR / "SKILL.md",
            REVIEW_SKILL_DIR / "prompts" / "normalize.md",
            REVIEW_SKILL_DIR / "prompts" / "semantic.md",
            REVIEW_SKILL_DIR / "bin" / "jira-fetch.mjs",
            REVIEW_SKILL_DIR / "bin" / "rulecheck.mjs",
            REVIEW_SKILL_DIR / "bin" / "report.mjs",
            REVIEW_SKILL_DIR / "bin" / "verify-semantic-quotes.mjs",
            REVIEW_SKILL_DIR / "bin" / "lib" / "attachments.mjs",
        ]
        for path in expected:
            self.assertTrue(path.is_file(), f"缺少技能文件：{path}")

    def test_review_skill_is_registered_as_usable_skill(self):
        """技能目录需满足注册表约定：front matter 的 name 等于目录名。"""
        from app.skillrun.registry import load_skill

        skill = load_skill(REVIEW_SKILL_DIR, "review-jira-req")

        self.assertEqual(skill.skill_id, "review-jira-req")
        self.assertTrue(skill.description)

    def test_review_skill_documents_container_adaptation(self):
        """新技能需声明容器内的技能目录、凭据桥接与只读边界。"""
        text = (REVIEW_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("/data/skills/review-jira-req", text)
        self.assertIn('export JIRA_PAT="$JIRA_TOKEN"', text)
        self.assertIn("只读边界不变", text)

    def test_review_skill_supports_attachment_evidence(self):
        """新版技能需在拉取、语义评审与报告中贯通附件证据。"""
        fetch_source = (REVIEW_SKILL_DIR / "bin" / "jira-fetch.mjs").read_text(encoding="utf-8")
        report_source = (REVIEW_SKILL_DIR / "bin" / "report.mjs").read_text(encoding="utf-8")
        semantic_source = (REVIEW_SKILL_DIR / "prompts" / "semantic.md").read_text(encoding="utf-8")

        self.assertIn("downloadAttachments", fetch_source)
        self.assertIn("--max-attachments", fetch_source)
        self.assertIn("附件检查总览", report_source)
        self.assertIn("attachment:<id>", semantic_source)

    def test_review_skill_scripts_stay_read_only(self):
        """脚本层不得出现写请求，保持“拉取只读”的硬约束。"""
        fetch_source = (REVIEW_SKILL_DIR / "bin" / "jira-fetch.mjs").read_text(encoding="utf-8")

        for method in ("POST", "PUT", "DELETE"):
            self.assertNotIn(f"'{method}'", fetch_source)
            self.assertNotIn(f'"{method}"', fetch_source)


class ReportAttachmentContractTests(unittest.TestCase):
    def test_gate_skill_uploads_report_as_attachment(self):
        """jira-gate-1 需写明调用新技能、上传附件，且文件名以“宋立志”结尾。"""
        text = (GATE_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("### 11. 评审报告（review-jira-req）与附件上传", text)
        self.assertIn("/data/skills/review-jira-req", text)
        self.assertIn("<KEY>-需求评审报告-宋立志.md", text)
        self.assertIn("必须以「宋立志」结尾", text)
        self.assertIn("jira-cli.mjs attach", text)
        # 旧的“唯一交付物是评论”口径必须已让位于“评论 + 附件”。
        self.assertNotIn("唯一合规交付物是 Jira 评论", text)

    def test_upload_attachment_supports_custom_filename(self):
        """附件上传需支持 --name 改名，并让 fetch 自行生成 multipart 边界。"""
        source = (GATE_SKILL_DIR / "scripts" / "jira.mjs").read_text(encoding="utf-8")
        cli_source = (GATE_SKILL_DIR / "scripts" / "jira-cli.mjs").read_text(encoding="utf-8")

        self.assertIn("export async function uploadAttachment", source)
        self.assertIn("/attachments`", source)
        self.assertIn("options.body instanceof FormData", source)
        self.assertIn('case "attach"', cli_source)
        self.assertIn("--name <附件名>", cli_source)

    def test_attachment_api_and_permission_are_documented(self):
        """接口文档需覆盖附件上传接口与 CREATE_ATTACHMENTS 权限。"""
        text = (GATE_SKILL_DIR / "references" / "jira-api.md").read_text(encoding="utf-8")

        self.assertIn("/rest/api/2/issue/{KEY}/attachments", text)
        self.assertIn("CREATE_ATTACHMENTS", text)
        self.assertIn("## 10. 评审报告附件上传", text)

    def test_attach_failure_is_visible_via_exit_code(self):
        """上传失败必须置退出码 1 并在 stderr 告警，避免只看 exit code 就当成成功。"""
        cli_source = (GATE_SKILL_DIR / "scripts" / "jira-cli.mjs").read_text(encoding="utf-8")

        self.assertIn("process.exitCode = 1", cli_source)
        self.assertIn("[附件上传失败]", cli_source)
        self.assertIn('case "attach-list"', cli_source)

    def test_skill_requires_readback_after_upload(self):
        """SKILL.md 必须要求上传后读回确认，成功判定不看退出码。"""
        text = (GATE_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("上传后必须读回确认", text)
        self.assertIn("attach-list", text)
        self.assertIn("不要把失败说成成功", text)

    def test_execution_order_puts_transition_last(self):
        """执行顺序固定为 评论 → 门禁字段 → 附件 → 流转，流转必须是最后一个动作。"""
        skill_text = (GATE_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        doc_text = (GATE_SKILL_DIR / "references" / "jira-api.md").read_text(encoding="utf-8")

        skill_order = "写评论（含 @）→ 回写门禁字段 → 上传附件 → 流转状态"
        doc_order = "评论（含 @）→ 门禁字段 → 附件 → 流转"
        self.assertIn(skill_order, skill_text)
        self.assertIn(doc_order, doc_text)
        # 旧顺序（附件在流转之后）不得残留。
        self.assertNotIn("流转状态（仅不达标）→ 上传附件", skill_text)
        self.assertNotIn("流转状态（仅不达标）→ 上传附件", doc_text)

    def test_comment_template_ends_with_attachment_line(self):
        """评论末尾必须固定带一行「报告附件」，达标与不达标都要有。"""
        skill_text = (GATE_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        doc_text = (GATE_SKILL_DIR / "references" / "jira-api.md").read_text(encoding="utf-8")

        self.assertIn("* 报告附件：{{<KEY>-需求评审报告-宋立志.md}}", skill_text)
        self.assertIn("评论末尾固定的一行，达标与不达标都要有", skill_text)
        self.assertIn("报告附件：<KEY>-需求评审报告-宋立志.md（已上传）", doc_text)
class ReportConclusionContractTests(unittest.TestCase):
    """评论末尾「评审结论」契约：取值只能从报告 md 里确定性提取，不许模型自己判。"""

    def test_comment_line_appends_conclusion_after_attachment(self):
        """评论末尾固定行需在附件之后追加「｜评审结论：」，并限定三个取值。"""
        skill_text = (GATE_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        doc_text = (GATE_SKILL_DIR / "references" / "jira-api.md").read_text(encoding="utf-8")

        self.assertIn("｜评审结论：{{不合格 / 需修改 / 合格}}", skill_text)
        self.assertIn("｜评审结论：需修改", skill_text)
        self.assertIn("｜评审结论：<不合格/需修改/合格>", doc_text)

    def test_conclusion_script_is_shipped_and_restricted(self):
        """提取脚本需随技能分发，只能输出三个取值，异常一律以退出码 1 结束。"""
        script = GATE_SKILL_DIR / "scripts" / "report-conclusion.mjs"

        self.assertTrue(script.is_file(), f"缺少技能脚本：{script}")
        source = script.read_text(encoding="utf-8")
        self.assertIn('const ALLOWED = ["不合格", "需修改", "合格"];', source)
        self.assertIn("process.exitCode = 1", source)
        self.assertIn("[提取评审结论失败]", source)
        # 报告里每个 issue 小节固定含「- 结论：**X**」，提取必须以它为准。
        self.assertIn("结论：\\*\\*", source)

    def test_docs_forbid_guessing_the_conclusion(self):
        """文档需写明结论由脚本提取、报告须先跑完流水线，且不许自己判。"""
        skill_text = (GATE_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        doc_text = (GATE_SKILL_DIR / "references" / "jira-api.md").read_text(encoding="utf-8")

        self.assertIn("不许自己猜", skill_text)
        self.assertIn("这一步要在写评论之前做完", skill_text)
        self.assertIn("五步流水线必须在写评论之前跑到第 5 步", skill_text)
        self.assertIn("它是唯一来源，不许自己判", doc_text)
        self.assertIn("五步流水线必须在写评论之前跑到第 5 步", doc_text)

    def test_script_extracts_conclusion_from_real_report_shape(self):
        """按 report.mjs 的真实小节结构喂样例：能取到结论、无 KEY 时拒绝歧义、缺 KEY 时报错。"""
        script = GATE_SKILL_DIR / "scripts" / "report-conclusion.mjs"
        if not shutil.which("node"):
            self.skipTest("未安装 node，跳过提取脚本的实跑校验")

        sample = "\n".join(
            [
                "## 需求逐条",
                "",
                "### SHOP-101 消息发送失败重试",
                "",
                "- 结论：**合格**",
                "- 类型 / 状态：需求 / 评审中",
                "",
                "### SHOP-102 订单导出",
                "",
                "- 结论：**需修改**",
                "",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "review-report.md"
            report.write_text(sample, encoding="utf-8")

            matched = self.run_script(script, str(report), "SHOP-102")
            self.assertEqual(matched.returncode, 0, matched.stderr)
            self.assertEqual(matched.stdout.strip(), "需修改")

            ambiguous = self.run_script(script, str(report))
            self.assertEqual(ambiguous.returncode, 1, "多 issue 且未指定 KEY 时必须报错")
            self.assertIn("请用 KEY 参数指定", ambiguous.stderr)

            missing = self.run_script(script, str(report), "SHOP-999")
            self.assertEqual(missing.returncode, 1, "找不到该 KEY 的小节时必须报错")
            self.assertIn("[提取评审结论失败]", missing.stderr)

            illegal = Path(tmp) / "bad-report.md"
            illegal.write_text("### SHOP-101 结构变了的报告\n\n- 结论：**待评审**\n", encoding="utf-8")
            rejected = self.run_script(script, str(illegal), "SHOP-101")
            self.assertEqual(rejected.returncode, 1, "取值不在三档内时必须报错")
            self.assertIn("结论取值非法", rejected.stderr)

    @staticmethod
    def run_script(script, *args):
        """同步执行提取脚本，返回带 returncode / stdout / stderr 的结果。"""
        return subprocess.run(
            ["node", str(script), *args],
            capture_output=True,
            encoding="utf-8",
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
