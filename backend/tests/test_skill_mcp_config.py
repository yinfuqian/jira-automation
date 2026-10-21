"""流水线 MCP 配置契约测试：写进 CODEX_HOME/config.toml 的托管块必须合法、幂等、可撤销。"""
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.skillrun import mcp_config  # noqa: E402


class McpConfigTests(unittest.TestCase):
    """mcp_config.ensure_devops_mcp_config：托管块的写入、幂等、清理与转义。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.config_path = self.home / "config.toml"

    def read_config(self) -> dict:
        """读取并解析 CODEX_HOME/config.toml，顺带验证文档是合法 TOML。"""
        return tomllib.loads(self.config_path.read_text(encoding="utf-8"))

    def test_writes_valid_toml_with_token_header(self):
        """写入的配置必须能被 TOML 解析，并带上 X-User-Tokens 请求头。"""
        result = mcp_config.ensure_devops_mcp_config(
            self.home, "https://devops.ks1.wezhuiyi.com/mcp", "token-abcdefgh"
        )

        self.assertTrue(result["written"])
        server = self.read_config()["mcp_servers"]["pipeline-integration-mcp"]
        self.assertEqual(server["url"], "https://devops.ks1.wezhuiyi.com/mcp")
        self.assertEqual(server["http_headers"]["X-User-Tokens"], "token-abcdefgh")

    def test_only_pipeline_skills_receive_mcp(self):
        """流水线 MCP 只对明确需要它的技能开放，避免其它任务看到无关工具。"""
        self.assertTrue(mcp_config.supports_devops_mcp("jira-code"))
        self.assertTrue(mcp_config.supports_devops_mcp("devops-mcp-invoker"))
        self.assertFalse(mcp_config.supports_devops_mcp("jira-gate-bug"))
        self.assertFalse(mcp_config.supports_devops_mcp(""))

    def test_keeps_existing_config_untouched(self):
        """文件里原有的其它配置必须保持原样，只追加托管块。"""
        self.config_path.write_text("[other]\nkey = 1\n", encoding="utf-8")

        mcp_config.ensure_devops_mcp_config(self.home, "https://mcp/x", "token-abcdefgh")

        document = self.read_config()
        self.assertEqual(document["other"]["key"], 1)
        self.assertIn("pipeline-integration-mcp", document["mcp_servers"])

    def test_repeated_registration_is_idempotent(self):
        """重复注册不应该产生重复的 server 段。"""
        for _ in range(3):
            mcp_config.ensure_devops_mcp_config(self.home, "https://mcp/x", "token-abcdefgh")

        text = self.config_path.read_text(encoding="utf-8")
        self.assertEqual(text.count("[mcp_servers.pipeline-integration-mcp]"), 1)

    def test_token_rotation_replaces_old_value(self):
        """换令牌时托管块被整段替换，不残留旧令牌。"""
        mcp_config.ensure_devops_mcp_config(self.home, "https://mcp/x", "old-token-1234")

        mcp_config.ensure_devops_mcp_config(self.home, "https://mcp/x", "new-token-5678")

        text = self.config_path.read_text(encoding="utf-8")
        self.assertNotIn("old-token-1234", text)
        self.assertEqual(
            self.read_config()["mcp_servers"]["pipeline-integration-mcp"]["http_headers"]["X-User-Tokens"],
            "new-token-5678",
        )

    def test_missing_token_removes_managed_block(self):
        """未配置令牌时清理托管块，且不动文件里的其它内容。"""
        self.config_path.write_text("[other]\nkey = 2\n", encoding="utf-8")
        mcp_config.ensure_devops_mcp_config(self.home, "https://mcp/x", "token-abcdefgh")

        result = mcp_config.ensure_devops_mcp_config(self.home, "https://mcp/x", "")

        self.assertFalse(result["written"])
        self.assertEqual(result["reason"], "token-missing-block-removed")
        document = self.read_config()
        self.assertNotIn("mcp_servers", document)
        self.assertEqual(document["other"]["key"], 2)

    def test_token_with_quotes_is_escaped(self):
        """令牌里出现引号或反斜杠时仍要生成合法 TOML。"""
        tricky = chr(34).join(["tok", "with", "quote"]) + "\\tail"

        mcp_config.ensure_devops_mcp_config(self.home, "https://mcp/x", tricky)

        server = self.read_config()["mcp_servers"]["pipeline-integration-mcp"]
        self.assertEqual(server["http_headers"]["X-User-Tokens"], tricky)

    def test_missing_codex_home_is_skipped(self):
        """没有 CODEX_HOME 时直接跳过，不报错、不写文件。"""
        result = mcp_config.ensure_devops_mcp_config("", "https://mcp/x", "token-abcdefgh")

        self.assertFalse(result["written"])
        self.assertEqual(result["reason"], "codex-home-missing")

    def test_uses_documented_sub_table_form(self):
        """请求头按平台文档写成子表，方便运维直接对照文档核对。"""
        mcp_config.ensure_devops_mcp_config(self.home, "https://mcp/x", "token-abcdefgh")

        text = self.config_path.read_text(encoding="utf-8")
        self.assertIn("[mcp_servers.pipeline-integration-mcp.http_headers]", text)
        self.assertIn('X-User-Tokens = "token-abcdefgh"', text)

    def test_replaces_manually_added_server_table(self):
        """用户按平台文档手工加过同名表时，不能直接追加——那样会产生重复表，配置直接非法。"""
        self.config_path.write_text(
            "\n".join(
                [
                    "[other]",
                    "key = 1",
                    "",
                    "[mcp_servers.pipeline-integration-mcp]",
                    'url = "https://old.example.com/mcp"',
                    "",
                    "[mcp_servers.pipeline-integration-mcp.http_headers]",
                    'X-User-Tokens = "manual-old-token"',
                    "",
                    "[unrelated]",
                    "keep = true",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        mcp_config.ensure_devops_mcp_config(self.home, "https://mcp/x", "token-abcdefgh")

        document = self.read_config()
        self.assertEqual(document["other"]["key"], 1)
        self.assertTrue(document["unrelated"]["keep"])
        server = document["mcp_servers"]["pipeline-integration-mcp"]
        self.assertEqual(server["url"], "https://mcp/x")
        self.assertEqual(server["http_headers"]["X-User-Tokens"], "token-abcdefgh")
        text = self.config_path.read_text(encoding="utf-8")
        self.assertNotIn("manual-old-token", text)
        self.assertNotIn("old.example.com", text)

    def test_token_with_newline_still_produces_valid_toml(self):
        """令牌里混进换行时不能写出非法 TOML，否则 Codex 直接起不来。"""
        mcp_config.ensure_devops_mcp_config(self.home, "https://mcp/x", "token-\nabcdefgh\n")

        server = self.read_config()["mcp_servers"]["pipeline-integration-mcp"]
        self.assertEqual(server["http_headers"]["X-User-Tokens"], "token-abcdefgh")


if __name__ == "__main__":
    unittest.main()
