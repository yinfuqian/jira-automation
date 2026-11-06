"""技能流转契约测试：锁定 G1「通过不流转、退回补充→需求设计」与故事类型门禁。"""
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
PROTOTYPE_SCRIPT_PATH = SKILL_DIR / "scripts" / "detect-prototype.mjs"
API_DOC_PATH = SKILL_DIR / "references" / "jira-api.md"
CLEANUP_SCRIPT_PATH = SKILL_DIR / "scripts" / "workdir-cleanup.mjs"
NODE = shutil.which("node")


class SkillJiraGateFlowTests(unittest.TestCase):
    def test_skill_documents_return_flow_and_type_gate(self):
        """SKILL.md 必须写明通过不流转、退回补充流转到「需求设计」，并且只处理「故事」类型。"""
        text = SKILL_PATH.read_text(encoding="utf-8")

        self.assertIn("### 9. 交付顺序与「仅退回补充时流转并 @ 流转人」", text)
        self.assertIn("需求设计", text)
        self.assertIn("--to 需求设计", text)
        self.assertIn("通过不流转", text)
        self.assertIn("业务待决策事项", text)
        self.assertNotIn("决策卡", text)
        self.assertIn("只处理类型为「故事」", text)
        self.assertIn("precheck", text)
        self.assertNotIn("--to 开发设计中", text)
        self.assertNotIn("--to 评审中", text)
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
        self.assertIn("需求设计", payload["prompt_template"])
        self.assertIn("结论「通过」不流转状态", payload["prompt_template"])
        self.assertIn("review-jira-req", payload["prompt_template"])
        self.assertIn("review-jira-design", payload["prompt_template"])
        self.assertIn("review-jira-qa", payload["prompt_template"])
        self.assertIn("prepare.mjs", payload["prompt_template"])
        self.assertIn("productSupported", payload["prompt_template"])
        self.assertIn("Bot/Call/See/Pal/Learn/Aiforce", payload["prompt_template"])
        self.assertIn("scan.mjs", payload["prompt_template"])
        self.assertIn("candidates", payload["prompt_template"])
        self.assertIn("REUSED", payload["prompt_template"])
        self.assertIn("--coverage-required", payload["prompt_template"])
        self.assertIn("调用顺序固定为 review-jira-req → review-jira-design（仅单子已上传原型时）→ review-jira-qa", payload["prompt_template"])
        self.assertIn("build-report.mjs", payload["prompt_template"])
        self.assertIn("只执行阶段一", payload["prompt_template"])
        self.assertIn("ok=false", payload["prompt_template"])
        # QA 0.1.5 统一用 status 表示异常/不适用，G1 不能把退出码当成功依据。
        self.assertIn("status=SKIPPED", payload["prompt_template"])
        self.assertIn("status=ERROR", payload["prompt_template"])
        self.assertIn("exit 0", payload["prompt_template"])
        self.assertIn("不适用", payload["prompt_template"])
        self.assertIn("未完成", payload["prompt_template"])
        self.assertNotIn("--non-blocking", payload["prompt_template"])
        self.assertNotIn("failedStep", payload["prompt_template"])
        self.assertIn("继续写 G1 评论", payload["prompt_template"])

    def test_attachment_pruning_keeps_latest_and_preserves_comments(self):
        """G1 自己生成的两族附件只留最新一份，历史评论与产品附件不能被清理。"""
        text = SKILL_PATH.read_text(encoding="utf-8")
        payload = json.loads((SKILL_DIR / "runtime.json").read_text(encoding="utf-8"))
        jira_source = JIRA_SCRIPT_PATH.read_text(encoding="utf-8")
        cli_source = CLI_PATH.read_text(encoding="utf-8")

        self.assertIn("attach-prune", text)
        self.assertIn("--keep <新附件ID>", text)
        self.assertIn("历史评论一律保留、不删除也不改写", text)
        self.assertIn("不得删除产品上传的 PRD、原型或 QA 自己的清单", text)
        self.assertIn("attach-prune", payload["prompt_template"])
        self.assertIn("只保留最新一份", payload["prompt_template"])
        self.assertIn("历史评论必须保留", payload["prompt_template"])
        for name in ("deleteAttachment", "pruneAttachments"):
            self.assertIn(f"export async function {name}", jira_source)
        self.assertIn("export function selectStaleAttachments", jira_source)
        self.assertIn('case "attach-prune"', cli_source)

    @unittest.skipUnless(NODE, "未安装 node，跳过附件清理纯函数测试")
    def test_select_stale_attachments_keeps_latest_only(self):
        """同族附件里排除 keepIds，且后缀与其他材料不应被误删。"""
        jira_url = JIRA_SCRIPT_PATH.as_uri()
        script = (
            'const m = await import("%s");\n'
            'const items = [\n'
            '  { id: 1, filename: "BOT-1-需求评审报告-宋立志.md" },\n'
            '  { id: 2, filename: "BOT-1-需求评审报告-20261001.md" },\n'
            '  { id: 3, filename: "BOT-1-需求评审报告-宋立志.md" },\n'
            '  { id: 4, filename: "BOT-1-需求评审报告.html" },\n'
            '  { id: 5, filename: "BOT-1-PRD.md" },\n'
            '  { id: 6, filename: "BOT-1-QA需求准入门禁清单.md" },\n'
            '];\n'
            'const picked = m.selectStaleAttachments(items, { contains: ["需求评审报告"], suffix: ".md", keepIds: ["3"] });\n'
            'console.log(JSON.stringify(picked.map((item) => String(item.id))));\n'
        ) % jira_url
        result = subprocess.run(
            [NODE, "-e", script],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["1", "2"])

    @unittest.skipUnless(NODE, "未安装 node，跳过产品门禁脚本测试")
    def test_precheck_requires_supported_product(self):
        """G1 只放行「故事 + 六个产品之一」，其余一律 supported=false。"""
        jira_url = JIRA_SCRIPT_PATH.as_uri()
        script = (
            'const m = await import("%s");\n'
            'const run = (issue) => m.precheckIssueType("X-1", { issue });\n'
            'const ok = await run({ key: "X-1", issueType: "故事", status: "", summary: "", customFields: { "产品归属": "Learn" } });\n'
            'const wrongProduct = await run({ key: "X-2", issueType: "故事", status: "", summary: "", customFields: { "产品归属": "Eco" } });\n'
            'const missingProduct = await run({ key: "X-3", issueType: "故事", status: "", summary: "", customFields: {} });\n'
            'console.log(JSON.stringify([ok.supported, wrongProduct.supported, missingProduct.supported]));\n'
        ) % jira_url
        result = subprocess.run(
            [NODE, "-e", script], capture_output=True, encoding="utf-8", errors="replace", check=False, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [True, False, False])

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
        # QA 输入不适用/运营性失败都不能阻塞 G1 后续交付与流转。
        self.assertIn("status=SKIPPED", text)
        self.assertIn("status=ERROR", text)
        self.assertIn("未完成（status/errorCode/error）", text)
        self.assertIn("继续执行 G1 的评论", text)
        # QA 0.1.18：prepare 合并准备步骤，READY 后扫描候选、写 13 项判据并强制校验。
        self.assertIn("prepare.mjs", text)
        self.assertIn("REUSED", text)
        self.assertIn("scan.mjs", text)
        self.assertIn("candidates.json", text)
        self.assertIn("13 项", text)
        self.assertIn("reuse.mjs", text)
        self.assertIn("coverage.mjs", text)
        self.assertIn("--coverage-required", text)
        self.assertNotIn("--non-blocking", text)
        self.assertNotIn("failedStep", text)

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
        # design 1.2 起不采集截图，G1 的调用说明必须同步，不能再引用已删除的截图脚本。
        self.assertIn("不采集截图", text)
        self.assertNotIn("capture_prototype_evidence", text)
        self.assertNotIn("截图未验证", payload["prompt_template"])
        # 调用顺序必须是 req → design → qa，且都在 G1 评论之前。
        req_pos = text.index("### 11. 评审报告（review-jira-req）")
        design_pos = text.index("### 11.1 原型设计准入报告（review-jira-design）")
        qa_pos = text.index("### 11.2 QA 需求准入门禁（review-jira-qa）")
        self.assertLess(req_pos, design_pos)
        self.assertLess(design_pos, qa_pos)
        self.assertIn("review-jira-design", payload["prompt_template"])
        # 没有上传原型时必须跳过设计审核，不能把「缺原型」当业务阻断。
        self.assertIn("detect-prototype.mjs", text)
        self.assertIn("hasPrototype=true", text)
        self.assertIn("不适用（本单未上传原型）", text)
        self.assertIn("仅当单子已上传原型材料时", text)
        self.assertIn("detect-prototype.mjs", payload["prompt_template"])
        self.assertIn("hasPrototype=false", payload["prompt_template"])

    def test_design_review_is_skipped_without_prototype(self):
        """SKILL.md 必须写明无原型时跳过 review-jira-design，且不影响 QA 与后续交付。"""
        text = SKILL_PATH.read_text(encoding="utf-8")

        self.assertIn("### 3.1 原型材料判定（决定是否调用 review-jira-design）", text)
        self.assertIn("`hasPrototype=false` 且 `ambiguous` 为空 → **跳过 §11.1**", text)
        self.assertIn("不调用 `review-jira-design`", text)
        self.assertIn("继续 §11.2", text)
        self.assertIn("不适用（本单未上传原型）", text)
        # 无原型时不能生成/上传设计报告，也不能算设计业务不通过。
        self.assertIn("未上传原型记「不适用」不参与判定", text)
        # 历史回写的设计报告不能被当成业务原型，否则每轮都会自我触发设计审核。
        self.assertIn("先排除自生成的历史产物", text)
        self.assertIn("不得把「找不到业务原型」写成「审核未完成」", text)

    def test_delivery_order_runs_three_review_skills_before_comment(self):
        """三个评审技能必须在 G1 评论前完成，QA 评论/HTML 由 QA 技能自身交付。"""
        text = SKILL_PATH.read_text(encoding="utf-8")

        self.assertIn("review-jira-req` 五步流水线 → `review-jira-design` 原型设计准入（仅单子已上传原型时）→ `review-jira-qa` 阶段一", text)
        self.assertIn("QA 自己的评论和 HTML 上传由 `review-jira-qa` 在其阶段一内完成", text)
        self.assertIn("不要把 QA 的 HTML 再上传一次", text)
        self.assertIn("上传需求评审报告与已生成的设计准入报告附件", text)

    def test_child_skill_failures_never_interrupt_g1(self):
        """三个子技能的任何失败都只能记为「未完成」，不得中断 G1 后续交付。"""
        text = SKILL_PATH.read_text(encoding="utf-8")
        payload = json.loads((SKILL_DIR / "runtime.json").read_text(encoding="utf-8"))
        api_text = API_DOC_PATH.read_text(encoding="utf-8")

        self.assertIn("### 6.1 子技能失败统一收口（硬约束）", text)
        self.assertIn("所有子技能都不得中断 G1 流程", text)
        self.assertIn("子技能失败**不等于** G1 交付失败", text)
        self.assertIn("五步流水线任一步非零退出", text)
        self.assertIn("设计准入记为「未完成（原因）」", text)
        self.assertIn("QA 的任何失败", text)
        self.assertIn("只有 G1 自身无法形成业务结论", text)
        # 服务端提示词必须同样约束，避免只改文档不改实际执行。
        self.assertIn("所有子技能都不得中断 G1 流程", payload["prompt_template"])
        self.assertIn("review-jira-req 五步中任一步失败", payload["prompt_template"])
        self.assertIn("子技能失败但 G1 交付动作已完成", payload["prompt_template"])
        # 接口文档同步收口，不得再出现「停止 G1 后续交付」的旧口径。
        self.assertIn("需求评审记为「未完成（阶段/原因）」", api_text)
        self.assertIn("不中断 G1 后续交付", api_text)
        self.assertNotIn("停止 G1 后续交付并保留现场", api_text)

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
        self.assertIn("--to 需求设计", text)
        self.assertIn("通过不流转", text)

    def test_flow_owner_defaults_to_assignee(self):
        """流转人默认口径为经办人，文档与脚本必须一致地声明这一点。"""
        skill_text = SKILL_PATH.read_text(encoding="utf-8")
        doc_text = API_DOC_PATH.read_text(encoding="utf-8")

        self.assertIn("口径就是经办人", skill_text)
        self.assertIn("取经办人 `assignee`", doc_text)


class WorkdirCleanupDocTests(unittest.TestCase):
    """SKILL.md 必须写明收尾清理的范围、幂等性与「仅 G1 自身交付失败保留现场」。"""

    def test_skill_documents_cleanup_step(self):
        text = SKILL_PATH.read_text(encoding="utf-8")

        self.assertIn("### 12. 收尾：清理本次任务的工作区中间产物", text)
        self.assertIn("workdir-cleanup.mjs --key <KEY>", text)
        self.assertIn("`reports/` 不在清理范围内", text)
        self.assertIn("子技能失败但 G1 已完成评论", text)
        self.assertNotIn("交付失败（异常中断）时不要调用本脚本", text)
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


@unittest.skipUnless(NODE, "未安装 node，跳过原型材料判定测试")
class PrototypeDetectionTests(unittest.TestCase):
    """原型材料判定要能区分 PRD、原型图、原型 HTML 与未展开压缩包。"""

    def detect(self, files):
        """在临时目录里写入给定文件（支持子目录），返回脚本输出的 JSON。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, content in files.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            result = subprocess.run(
                [NODE, str(PROTOTYPE_SCRIPT_PATH), "--dir", str(root), "--json"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_prd_only_is_not_prototype(self):
        """只有 PRD（md/html）时判定为没有原型，不触发设计审核。"""
        payload = self.detect({
            "PRD_DELIVERY.md": "# PRD",
            "PRD.html": "<html><title>PRD 需求说明</title></html>",
        })

        self.assertFalse(payload["hasPrototype"])
        self.assertEqual(payload["hits"], [])

    def test_image_counts_as_prototype(self):
        """原型截图通常以图片附件上传，必须识别为原型材料。"""
        payload = self.detect({"image-2026.png": "x"})

        self.assertTrue(payload["hasPrototype"])
        self.assertEqual(payload["hits"][0]["kind"], "image")

    def test_html_keyword_in_name_or_content(self):
        """原型 HTML 可能靠文件名或正文关键词识别（含解压后的子目录）。"""
        by_name = self.detect({"Prototype.html": "<html></html>"})
        by_content = self.detect({"page.html": "<html><title>交互原型</title></html>"})
        by_nested_path = self.detect({"__unpacked/附件/原型/index.html": "<html></html>"})

        for payload in (by_name, by_content, by_nested_path):
            self.assertTrue(payload["hasPrototype"], payload)

    def test_unlabeled_html_and_archive_need_manual_check(self):
        """无关键词的 HTML 与未展开压缩包不能算命中，但要进入 ambiguous 提醒确认。"""
        payload = self.detect({
            "PRD.html": "<html><title>PRD</title></html>",
            "bundle.zip": "x",
        })

        self.assertFalse(payload["hasPrototype"])
        self.assertEqual([item["kind"] for item in payload["ambiguous"]], ["archive"])

    def test_historical_design_and_qa_reports_are_not_prototypes(self):
        """历史回写的设计报告/QA 清单即使文件名含「原型」也不能算业务原型。"""
        payload = self.detect({
            "BOT-39490-原型设计准入报告.html": "<html><title>原型设计准入报告</title></html>",
            "BOT-39490-QA需求准入门禁清单.html": "<html><title>QA 准入门禁清单</title></html>",
            "BOT-39490-需求评审报告-宋立志.md": "# 评审报告",
        })

        self.assertFalse(payload["hasPrototype"])
        self.assertEqual(payload["hits"], [])
        self.assertEqual(payload["ambiguous"], [])
        self.assertEqual(
            {item["kind"] for item in payload["ignored"]},
            {"self-artifact"},
        )

    def test_renamed_historical_report_is_excluded_by_content(self):
        """历史报告被重命名后，仍要按正文标题排除。"""
        payload = self.detect({
            "attachment-123.html": "<html><title>BOT-39490 原型设计准入报告</title></html>",
        })

        self.assertFalse(payload["hasPrototype"])
        self.assertEqual(payload["hits"], [])
        self.assertEqual([item["kind"] for item in payload["ignored"]], ["self-artifact-html"])

    def test_renamed_design_report_with_audit_title_is_excluded(self):
        """报告模板标题是「原型设计准入审核」，改名或旧命名上传后仍要排除。"""
        report = (
            "<!DOCTYPE html><html><head><title>原型设计准入审核 · 结果回调URL改造</title></head>"
            "<body><div class=\"eyebrow\">G1 · 原型设计准入审核</div><h1>结果回调URL改造</h1></body></html>"
        )

        for name in ("attachment-998877.html", "BOT-39490-原型设计准入审核.html"):
            with self.subTest(name=name):
                payload = self.detect({name: report})
                self.assertFalse(payload["hasPrototype"])
                self.assertEqual(payload["hits"], [])
                self.assertEqual(len(payload["ignored"]), 1)

    def test_business_topic_words_do_not_hide_real_prototype(self):
        """「评审报告」「PDCA」等业务主题词不能把真实原型判成历史产物。"""
        hit = self.detect({"BOT-39490-评审报告原型页面.html": "<html><title>评审报告页面</title></html>"})
        self.assertTrue(hit["hasPrototype"])
        self.assertEqual([item["kind"] for item in hit["hits"]], ["path-keyword"])
        self.assertEqual(hit["ignored"], [])

        manual = self.detect({"BOT-39490-PDCA看板.html": "<html><title>PDCA 看板</title></html>"})
        self.assertFalse(manual["hasPrototype"])
        self.assertEqual(manual["ignored"], [])
        self.assertEqual([item["kind"] for item in manual["ambiguous"]], ["html-unlabeled"])

    def test_business_prototype_still_triggers_design_review(self):
        """真实的业务原型（文件名或正文含原型）仍要触发设计审核。"""
        self.assertTrue(self.detect({"BOT-39490-交互原型.html": "<html></html>"})["hasPrototype"])
        self.assertTrue(self.detect({"attachment-9.html": "<html><title>页面交互原型</title></html>"})["hasPrototype"])

    def test_prd_html_mentioning_prototype_is_not_prototype(self):
        """PRD/需求说明正文提到「原型」时不能当成业务原型材料。"""
        payload = self.detect({
            "PRD.html": "<html><title>BOT-39490 需求说明</title><body>本次按交互原型实现</body></html>",
        })

        self.assertFalse(payload["hasPrototype"])
        self.assertEqual(payload["hits"], [])
        self.assertEqual(payload["ambiguous"], [])

    def test_untitled_html_mentioning_prototype_needs_manual_check(self):
        """标题无法判断、正文提到原型时进 ambiguous，交由调用方打开确认。"""
        payload = self.detect({
            "page.html": "<html><title>场景插件配置</title><body>本页为交互原型</body></html>",
        })

        self.assertFalse(payload["hasPrototype"])
        self.assertEqual([item["kind"] for item in payload["ambiguous"]], ["html-keyword"])


if __name__ == "__main__":
    unittest.main()
