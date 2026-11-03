"""Confluence REST 客户端与正文治理工具。

该模块只负责访问 Confluence、把存储格式正文转换成结构化纯文本，以及
切分检索单元。它不接触数据库，也不调用模型，便于用 Mock 做确定性测试。
"""
from __future__ import annotations

import hashlib
import html
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree
from urllib.parse import quote, urljoin, urlparse

import httpx


class ConfluenceError(RuntimeError):
    """Confluence 不可达、鉴权失败或返回非法数据。"""


@dataclass(frozen=True)
class ConfluencePage:
    """一次页面读取得到的不可变快照。"""

    page_id: str
    title: str
    space_key: str
    url: str
    version: int
    updated_at: datetime | None
    labels: tuple = ()
    body_html: str = ""
    content: str = ""

    @property
    def content_hash(self) -> str:
        """正文和标题的稳定指纹，用于判断索引是否需要重建。"""
        raw = f"{self.title}\n{self.content}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ConfluenceAttachment:
    """Confluence 页面附件的只读元数据。"""

    attachment_id: str
    title: str
    download_url: str
    media_type: str = ""
    file_size: int = 0


class _WikiHtmlParser(HTMLParser):
    """不做第三方依赖的 HTML 转文本器，保留标题与段落边界。"""

    BLOCK_TAGS = {
        "p", "div", "section", "article", "ul", "ol", "table", "tr", "blockquote", "pre"
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def _append_block(self, text=""):
        if text:
            self.parts.append(text)
        self.parts.append("\n\n")

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"script", "style"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in {"td", "th"}:
            self.parts.append(" | ")
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style"}:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} or tag in self.BLOCK_TAGS:
            self._append_block()

    def handle_data(self, data):
        if not self.skip_depth and data:
            self.parts.append(data)


def html_to_text(value) -> str:
    """把 Confluence storage HTML 转成保留标题层级的纯文本 Markdown。"""
    parser = _WikiHtmlParser()
    try:
        parser.feed(str(value or ""))
        parser.close()
    except Exception:
        return html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    compact = []
    blank = False
    for line in lines:
        if line:
            compact.append(line)
            blank = False
        elif compact and not blank:
            compact.append("")
            blank = True
    return "\n".join(compact).strip()


def _heading_level(line):
    match = re.match(r"^(#{1,6})\s+(.+)$", line)
    return (len(match.group(1)), match.group(2).strip()) if match else (0, "")


def chunk_text(text, max_chars=900, overlap_chars=120):
    """按标题和段落生成保留 heading_path 的检索切片。

    先按标题分段，再按段落累积；过长段落才按窗口切开。这样可以避免把
    表格、步骤列表和标题关系随意打散，同时限制单切片的模型输入体积。
    """
    max_chars = max(200, int(max_chars or 900))
    overlap_chars = max(0, min(int(overlap_chars or 0), max_chars // 3))
    lines = [line.rstrip() for line in str(text or "").splitlines()]
    sections = []
    headings = []
    buffer = []

    def flush():
        body = "\n".join(buffer).strip()
        if body:
            sections.append({"heading_path": " > ".join(headings), "content": body})
        buffer.clear()

    for line in lines:
        level, title = _heading_level(line)
        if level:
            flush()
            headings = headings[: max(0, level - 1)]
            headings.append(title)
            buffer.append(line)
        else:
            buffer.append(line if line else "")
    flush()

    if not sections:
        sections = [{"heading_path": "", "content": str(text or "").strip()}]

    chunks = []
    for section in sections:
        heading_path = section["heading_path"]
        paragraphs = re.split(r"\n\s*\n", section["content"])
        current = ""
        for paragraph in paragraphs:
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            while len(paragraph) > max_chars:
                if current:
                    chunks.append({"heading_path": heading_path, "content": current.strip()})
                    current = ""
                cut = paragraph[:max_chars]
                chunks.append({"heading_path": heading_path, "content": cut.strip()})
                paragraph = paragraph[max(0, max_chars - overlap_chars):].lstrip()
            candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
            if len(candidate) <= max_chars:
                current = candidate
                continue
            if current:
                chunks.append({"heading_path": heading_path, "content": current.strip()})
            current = paragraph
        if current:
            chunks.append({"heading_path": heading_path, "content": current.strip()})

    deduped = []
    seen = set()
    for item in chunks:
        content = item.get("content", "").strip()
        if not content:
            continue
        key = (item.get("heading_path", ""), content)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _docx_text(data):
    """从 DOCX 的 WordprocessingML 提取段落文本，不依赖 Office 运行时。"""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        try:
            xml_data = archive.read("word/document.xml")
        except KeyError as exc:
            raise ValueError("DOCX 缺少 word/document.xml") from exc
    root = ElementTree.fromstring(xml_data)
    parts = []
    for element in root.iter():
        local = element.tag.rsplit("}", 1)[-1]
        if local == "t" and element.text:
            parts.append(element.text)
        elif local == "tab":
            parts.append("\t")
        elif local in {"br", "cr"}:
            parts.append("\n")
        elif local == "p":
            parts.append("\n")
    return re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()


def extract_attachment_text(title, data, max_chars=120000):
    """提取可安全只读分析的附件文本；不支持的类型返回空字符串。"""
    name = str(title or "").lower()
    suffix = Path(name).suffix
    text = ""
    if suffix == ".docx":
        text = _docx_text(data)
    elif suffix in {".txt", ".md", ".csv", ".json", ".xml", ".log", ".yaml", ".yml"}:
        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
    return str(text or "").strip()[: max(0, int(max_chars or 0))]


class ConfluenceClient:
    """Confluence Server/Data Center REST API 的最小只读客户端。"""

    def __init__(self, settings, transport=None):
        self._settings = settings
        if not settings.configured:
            raise ConfluenceError("Confluence 地址或凭据未配置")
        parsed = urlparse(settings.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfluenceError("CONFLUENCE_URL 必须是合法的 http(s) 地址")
        headers = {"Accept": "application/json"}
        auth = None
        mode = str(settings.auth_mode or "bearer").strip().lower()
        if mode == "basic":
            auth = (settings.username, settings.password)
        else:
            headers["Authorization"] = f"Bearer {settings.token}"
        self._client = httpx.Client(
            base_url=settings.base_url,
            headers=headers,
            auth=auth,
            timeout=max(5, int(settings.request_timeout_seconds or 30)),
            verify=bool(settings.tls_verify),
            follow_redirects=True,
            transport=transport,
        )

    def close(self):
        """关闭底层连接池。"""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def _get(self, path, params=None):
        try:
            response = self._client.get(path, params=params or {})
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {401, 403}:
                raise ConfluenceError("Confluence 鉴权失败或当前账号无权访问") from exc
            if status == 404:
                raise ConfluenceError("Confluence 页面不存在或已删除") from exc
            raise ConfluenceError(f"Confluence 返回 HTTP {status}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ConfluenceError(f"Confluence 请求失败：{exc}") from exc

    @staticmethod
    def _version(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _updated_at(value):
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None

    def _normalize_page(self, raw):
        body_html = (((raw.get("body") or {}).get("storage") or {}).get("value") or "")
        space = raw.get("space") or {}
        version = raw.get("version") or {}
        links = raw.get("_links") or {}
        webui = str(links.get("webui") or "")
        url = urljoin(self._settings.base_url + "/", webui.lstrip("/")) if webui else ""
        labels = []
        for item in ((raw.get("metadata") or {}).get("labels") or {}).get("results") or []:
            name = str(item.get("name") or "").strip()
            if name:
                labels.append(name)
        return ConfluencePage(
            page_id=str(raw.get("id") or "").strip(),
            title=str(raw.get("title") or "").strip(),
            space_key=str(space.get("key") or "").strip(),
            url=url,
            version=self._version(version.get("number")),
            updated_at=self._updated_at(version.get("when")),
            labels=tuple(labels),
            body_html=body_html,
            content=html_to_text(body_html),
        )

    def get_page(self, page_id):
        """按页面 ID 读取当前版本、标签和正文。"""
        data = self._get(
            f"/rest/api/content/{quote(str(page_id), safe='')}",
            {"expand": "body.storage,version,space,metadata.labels"},
        )
        page = self._normalize_page(data)
        if not page.page_id:
            raise ConfluenceError("Confluence 返回的页面缺少 ID")
        return page

    def list_attachments(self, page_id):
        """列出页面附件；只返回可由当前账号只读下载的元数据。"""
        data = self._get(
            f"/rest/api/content/{quote(str(page_id), safe='')}/child/attachment",
            {"limit": 100, "expand": "version,metadata.labels"},
        )
        attachments = []
        for item in data.get("results") or []:
            links = item.get("_links") or {}
            download = str(links.get("download") or "").strip()
            extensions = item.get("extensions") or {}
            attachments.append(
                ConfluenceAttachment(
                    attachment_id=str(item.get("id") or "").strip(),
                    title=str(item.get("title") or "").strip(),
                    download_url=urljoin(self._settings.base_url + "/", download.lstrip("/")) if download else "",
                    media_type=str(extensions.get("mediaType") or "").strip(),
                    file_size=int(extensions.get("fileSize") or 0),
                )
            )
        return attachments

    def download_attachment(self, attachment, max_bytes=20 * 1024 * 1024):
        """下载附件到内存，超过安全上限时拒绝读取。"""
        if not attachment.download_url:
            raise ConfluenceError("Confluence 附件缺少下载地址")
        if attachment.file_size and attachment.file_size > int(max_bytes):
            raise ConfluenceError("Confluence 附件超过读取上限")
        try:
            response = self._client.get(attachment.download_url)
            response.raise_for_status()
            data = response.content
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                raise ConfluenceError("Confluence 附件鉴权失败或当前账号无权访问") from exc
            raise ConfluenceError(f"Confluence 附件返回 HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise ConfluenceError(f"Confluence 附件下载失败：{exc}") from exc
        if len(data) > int(max_bytes):
            raise ConfluenceError("Confluence 附件超过读取上限")
        return data

    def search_pages(self, cql, limit=100, start=0):
        """执行 CQL 搜索并返回网页正文快照。"""
        data = self._get(
            "/rest/api/content/search",
            {
                "cql": str(cql or "").strip(),
                "limit": max(1, min(100, int(limit or 100))),
                "start": max(0, int(start or 0)),
                "expand": "body.storage,version,space,metadata.labels",
            },
        )
        return [self._normalize_page(item) for item in data.get("results") or []], int(data.get("size") or 0)

    def iter_pages(self, cql, limit=5000):
        """分页读取搜索结果，达到上限后停止。"""
        total = 0
        start = 0
        while total < int(limit):
            batch, size = self.search_pages(cql, limit=100, start=start)
            if not batch:
                break
            for page in batch:
                if total >= int(limit):
                    return
                yield page
                total += 1
            # Confluence Data Center 可能把请求的 100 条稳定截断为 50 条。
            # 因此只有下一页返回空结果时才结束，不能拿请求上限或 size 判断。
            start += len(batch)


def build_scope_cql(space_keys=(), labels=(), root_page_ids=()):
    """把配置转换成 CQL；空空间表示当前账号可访问的全部页面。"""
    clauses = ["type = page"]
    spaces = [str(item).strip() for item in space_keys or () if str(item).strip()]
    label_values = [str(item).strip() for item in labels or () if str(item).strip()]
    roots = [str(item).strip() for item in root_page_ids or () if str(item).strip()]
    if spaces:
        clauses.append("(" + " OR ".join(f'space = "{item}"' for item in spaces) + ")")
    if label_values:
        clauses.append("(" + " OR ".join(f'label = "{item}"' for item in label_values) + ")")
    if roots:
        tree = [f'ancestor = "{item}"' for item in roots] + [f'id = "{item}"' for item in roots]
        clauses.append("(" + " OR ".join(tree) + ")")
    return " AND ".join(clauses) + " ORDER BY lastmodified DESC"


def build_question_cql(question, terms, space_keys=(), labels=(), root_page_ids=()):
    """在索引未命中时做一次受限的 Confluence 在线全文搜索。"""
    scope = build_scope_cql(space_keys, labels, root_page_ids).replace(" ORDER BY lastmodified DESC", "")
    terms = [
        re.sub(r'[\\\"]', " ", str(item)).strip()
        for item in terms or ()
        if str(item).strip()
    ]
    terms = list(dict.fromkeys(item for item in terms if item))
    if not terms:
        phrase = re.sub(r"[\\\"]", " ", str(question or "")).strip()
        terms = [phrase] if phrase else []
    if not terms:
        return scope

    product_terms = [item for item in terms if re.search(r"[a-z0-9]", item, re.IGNORECASE)]
    intent_terms = [item for item in terms if item not in product_terms]

    def match_group(values, wildcard=False):
        matches = []
        for item in values[:8]:
            literal = item[:80].replace('"', "")
            if wildcard and re.fullmatch(r"[a-z0-9][a-z0-9_.:/-]*", literal, re.IGNORECASE):
                literal = literal.rstrip("*") + "*"
            if literal:
                matches.append(f'(title ~ "{literal}" OR text ~ "{literal}")')
        if not matches:
            return ""
        return matches[0] if len(matches) == 1 else "(" + " OR ".join(matches) + ")"

    clauses = [match_group(product_terms, wildcard=True), match_group(intent_terms)]
    clauses = [item for item in clauses if item]
    if not clauses:
        return scope
    # 不指定 lastmodified 排序，让 Confluence 使用相关性排序；产品与问题
    # 意图同时约束，避免仅因命中 Call 就把值周记录排在部署指引前面。
    return f"{scope} AND " + " AND ".join(clauses)


_DOCUMENT_TERMS = ("部署文档", "部署指引", "安装手册", "操作手册", "指引", "手册", "文档")


def _title_match_group(field, values, wildcard=False):
    """生成标题匹配组；英文产品名使用前缀通配，兼容 call3.x 等版本命名。"""
    matches = []
    for value in values[:8]:
        literal = re.sub(r'[\\\"]', " ", str(value)).strip()[:80]
        if not literal:
            continue
        if wildcard and re.fullmatch(r"[a-z0-9][a-z0-9_.:/-]*", literal, re.IGNORECASE):
            literal = literal.rstrip("*") + "*"
        matches.append(f'{field} ~ "{literal}"')
    if not matches:
        return ""
    return matches[0] if len(matches) == 1 else "(" + " OR ".join(matches) + ")"


def build_question_cqls(question, terms, space_keys=(), labels=(), root_page_ids=()):
    """生成从精确到宽泛的候选查询，优先标题语义，正文检索仅作兜底。"""
    scope = build_scope_cql(space_keys, labels, root_page_ids).replace(" ORDER BY lastmodified DESC", "")
    values = [re.sub(r'[\\\"]', " ", str(item)).strip() for item in terms or () if str(item).strip()]
    values = list(dict.fromkeys(item for item in values if item))
    products = [item for item in values if re.search(r"[a-z0-9]", item, re.IGNORECASE)]
    intents = [item for item in values if item not in products]
    document_terms = [item for item in intents if item in _DOCUMENT_TERMS]
    action_terms = [item for item in intents if item not in document_terms]

    product_title = _title_match_group("title", products, wildcard=True)
    action_title = _title_match_group("title", action_terms)
    document_title = _title_match_group("title", document_terms)
    queries = []
    if product_title and action_title and document_title:
        queries.append(f"{scope} AND {product_title} AND {action_title} AND {document_title}")
    if product_title and (action_title or document_title):
        title_intent = "(" + " OR ".join(item for item in (action_title, document_title) if item) + ")"
        queries.append(f"{scope} AND {product_title} AND {title_intent}")
    if not product_title and action_title and document_title:
        queries.append(f"{scope} AND {action_title} AND {document_title}")

    fallback = build_question_cql(question, values, space_keys, labels, root_page_ids)
    if fallback and fallback not in queries:
        queries.append(fallback)
    return queries or [scope]
