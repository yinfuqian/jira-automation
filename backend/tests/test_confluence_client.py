"""Confluence 只读客户端与正文切片测试。"""
import sys
import unittest
import io
import zipfile
from pathlib import Path

import httpx


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.chatbot.confluence import (  # noqa: E402
    ConfluenceClient,
    ConfluenceError,
    build_question_cql,
    build_question_cqls,
    build_scope_cql,
    chunk_text,
    extract_attachment_text,
    html_to_text,
)
from app.chatbot.knowledge import key_terms  # noqa: E402
from app.chatbot.settings import ConfluenceSettings  # noqa: E402


class ConfluenceTextTests(unittest.TestCase):
    def test_storage_html_becomes_heading_aware_text(self):
        html = "<h1>发布流程</h1><p>先灰度。</p><ul><li>验证</li><li>放量</li></ul><script>x()</script>"

        text = html_to_text(html)

        self.assertIn("# 发布流程", text)
        self.assertIn("- 验证", text)
        self.assertNotIn("x()", text)

    def test_chunks_keep_heading_path_and_size_bound(self):
        content = "# 权限\n\n" + "\n\n".join(f"第{i}段权限检查说明" for i in range(40))

        chunks = chunk_text(content, max_chars=200, overlap_chars=20)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(item["heading_path"] == "权限" for item in chunks))
        self.assertTrue(all(len(item["content"]) <= 200 for item in chunks))

    def test_cql_limits_search_scope_and_question_terms(self):
        scope = build_scope_cql(["OPS"], ["runbook"], ["100"])
        terms = key_terms("灰度发布文档在哪里")
        question = build_question_cql("灰度发布文档在哪里", terms, ["OPS"])

        self.assertIn('space = "OPS"', scope)
        self.assertIn('label = "runbook"', scope)
        self.assertIn('ancestor = "100"', scope)
        self.assertIn('title ~ "灰度"', question)
        self.assertNotIn("ORDER BY", question)

    def test_question_keywords_remove_conversational_fillers(self):
        terms = key_terms("我现在想部署一个call  call 最新的部署文档在哪里")

        self.assertIn("call", terms)
        self.assertIn("部署", terms)
        self.assertIn("文档", terms)
        self.assertFalse(any("我现在" in item or "在想部署" in item for item in terms))

    def test_question_cql_requires_product_and_deployment_intent(self):
        query = "我现在想部署一个call  call 最新的部署文档在哪里"

        cql = build_question_cql(query, key_terms(query), ["CallKnowledgeBase"])

        self.assertIn('space = "CallKnowledgeBase"', cql)
        self.assertIn('(title ~ "call*" or text ~ "call*")', cql.lower())
        self.assertIn('title ~ "部署"', cql)
        self.assertIn('title ~ "文档"', cql)
        self.assertNotIn("ORDER BY", cql)

    def test_question_cqls_expand_product_family_and_prefer_title_documents(self):
        query = "我现在想部署一个call  call 最新的部署文档在哪里"

        cqls = build_question_cqls(query, key_terms(query))

        self.assertTrue(cqls)
        self.assertIn('title ~ "call*"', cqls[0])
        self.assertIn('title ~ "部署"', cqls[0])
        self.assertIn('title ~ "文档"', cqls[0])
        self.assertTrue(any('text ~ "call*"' in item for item in cqls))

    def test_docx_attachment_text_is_extracted_with_paragraphs(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "word/document.xml",
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:r><w:t>第一步</w:t></w:r></w:p>'
                '<w:p><w:r><w:t>部署服务</w:t></w:r></w:p></w:body></w:document>',
            )

        text = extract_attachment_text("部署指引.docx", buffer.getvalue())

        self.assertIn("第一步", text)
        self.assertIn("部署服务", text)


class ConfluenceHttpTests(unittest.TestCase):
    def _settings(self):
        return ConfluenceSettings(
            base_url="https://confluence.example",
            token="read-only-token",
            auth_mode="bearer",
        )

    def test_authentication_error_is_reported_without_leaking_token(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(401, json={"message": "denied"}))
        with ConfluenceClient(self._settings(), transport=transport) as client:
            with self.assertRaisesRegex(ConfluenceError, "鉴权失败"):
                client.get_page("100")

    def test_search_pages_parses_version_labels_and_body(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "size": 1,
                    "results": [
                        {
                            "id": "100",
                            "title": "发布手册",
                            "space": {"key": "OPS"},
                            "version": {"number": 7, "when": "2026-11-01T10:00:00.000+08:00"},
                            "metadata": {"labels": {"results": [{"name": "runbook"}]}},
                            "body": {"storage": {"value": "<h2>步骤</h2><p>先灰度</p>"}},
                            "_links": {"webui": "/display/OPS/page"},
                        }
                    ],
                },
            )

        transport = httpx.MockTransport(handler)
        with ConfluenceClient(self._settings(), transport=transport) as client:
            pages, size = client.search_pages("type = page")

        self.assertEqual(size, 1)
        self.assertEqual(pages[0].page_id, "100")
        self.assertEqual(pages[0].version, 7)
        self.assertEqual(pages[0].labels, ("runbook",))
        self.assertIn("先灰度", pages[0].content)

    def test_iter_pages_continues_when_server_caps_page_size_at_fifty(self):
        """接口请求 100 条但服务端只返回 50 条时，仍必须继续翻页。"""
        pages = [f"生成第 {index} 页部署文档" for index in range(120)]

        def handler(request):
            start = int(request.url.params.get("start") or 0)
            batch = pages[start:start + 50]
            return httpx.Response(
                200,
                json={
                    "size": len(batch),
                    "results": [
                        {
                            "id": str(start + offset),
                            "title": title,
                            "space": {"key": "OPS"},
                            "version": {"number": 1},
                            "body": {"storage": {"value": f"<p>{title}</p>"}},
                        }
                        for offset, title in enumerate(batch)
                    ],
                },
            )

        transport = httpx.MockTransport(handler)
        with ConfluenceClient(self._settings(), transport=transport) as client:
            imported = list(client.iter_pages("type = page", limit=120))

        self.assertEqual(len(imported), 120)
        self.assertEqual(imported[-1].title, "生成第 119 页部署文档")


if __name__ == "__main__":
    unittest.main()
