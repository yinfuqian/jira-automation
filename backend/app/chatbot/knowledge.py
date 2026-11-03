"""Confluence 索引同步、混合检索和在线新鲜度刷新。"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import uuid
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime

import httpx
from sqlalchemy import or_

from extensions import db

from .confluence import (
    ConfluenceClient,
    ConfluenceError,
    build_question_cqls,
    build_scope_cql,
    chunk_text,
    extract_attachment_text,
)
from .models.knowledge import KnowledgeChunk, KnowledgeDocument, KnowledgeSyncRun


logger = logging.getLogger(__name__)
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_.:/-]{1,}", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u3400-\u9fff]+")
_STOP_WORDS = {
    "的", "了", "是", "在", "和", "与", "及", "或", "有", "没有", "怎么", "如何", "什么",
    "为什么", "是否", "可以", "需要", "进行", "一个", "这个", "那个", "请问", "一下",
    "最新", "现在", "目前", "哪里", "文档吗",
    "the", "a", "an", "is", "are", "how", "what", "why", "to", "of", "and", "or",
}
_FILLER_PHRASES = (
    "我现在想", "我现在", "我想问", "我想", "麻烦帮我", "请帮我", "帮我", "请问",
    "最新的", "最新", "在哪里", "在哪儿", "哪里", "是什么", "怎么", "如何", "一个",
    "这个", "那个", "一下", "是否", "可以", "需要",
)
_INTENT_TERMS = (
    "登录信息", "登录账密", "登录账号", "登录密码", "用户名", "凭据", "账号", "密码",
    "域名", "访问地址", "端口", "namespace", "命名空间", "联系人", "版本信息", "配置信息",
    "部署指引", "部署文档", "部署步骤", "安装指引", "配置文档", "排查步骤",
    "部署", "发布", "上线", "安装", "配置", "升级", "回滚", "迁移", "接入",
    "排查", "排错", "报错", "异常", "故障", "问题", "日志", "监控", "告警",
    "超时", "错误码", "原因分析", "备份", "恢复", "处理", "解决", "权限",
    "流程", "步骤", "方法", "文档", "指引", "手册",
    "版本", "环境", "接口", "数据库", "服务", "启动", "重启", "地址", "端口",
    "使用", "操作", "验证", "测试", "构建", "编译", "检查", "方案",
)
_DOCUMENT_TERMS = ("部署文档", "部署指引", "安装手册", "操作手册", "指引", "手册", "文档")
_INFORMATION_TERMS = (
    "登录信息", "登录账密", "登录账号", "登录密码", "用户名", "凭据", "账号", "密码",
    "域名", "访问地址", "端口", "namespace", "命名空间", "联系人", "版本信息", "配置信息",
)


@dataclass
class KnowledgeHit:
    """一个供模型使用的页面切片及排序依据。"""

    document: KnowledgeDocument
    chunk: KnowledgeChunk
    score: float
    lexical_score: float = 0.0
    vector_score: float = 0.0


@dataclass(frozen=True)
class QueryIntent:
    """用户问题中与检索直接相关的结构化意图。"""

    product_terms: tuple
    action_terms: tuple
    document_terms: tuple
    information_terms: tuple
    wants_reference: bool
    wants_latest: bool

    @property
    def all_terms(self):
        return tuple(
            dict.fromkeys(
                self.product_terms
                + self.action_terms
                + self.document_terms
                + self.information_terms
            )
        )


def content_hash(value):
    """返回 UTF-8 文本的 SHA-256。"""
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def tokenize(value):
    """为中文和英文建立稳定检索词；中文使用单字与双字组合。"""
    text = str(value or "").lower()
    tokens = []
    for word in _WORD_RE.findall(text):
        if word not in _STOP_WORDS and len(word) > 1:
            tokens.append(word)
    for block in _CJK_RE.findall(text):
        if block in _STOP_WORDS:
            continue
        if len(block) == 1:
            tokens.append(block)
            continue
        tokens.extend(block[index:index + 2] for index in range(len(block) - 1))
    return tokens


def key_terms(value, limit=8):
    """提取产品和明确意图词，过滤口语填充词与无意义中文片段。"""
    text = str(value or "").strip()
    terms = []
    seen = set()

    def add(item):
        item = str(item or "").strip().lower()
        if not item or item in seen or item in _STOP_WORDS:
            return False
        seen.add(item)
        terms.append(item)
        return len(terms) >= int(limit)

    # 英文和数字通常就是产品名、模块名或版本号，必须优先保留。
    for word in _WORD_RE.findall(text.lower()):
        if word not in _STOP_WORDS and len(word) > 1:
            if add(word):
                return terms

    # 业务意图词比任意中文 n-gram 稳定，优先按它们在原问题中的位置加入。
    located = []
    lowered = text.lower()
    for term in _INTENT_TERMS:
        index = lowered.find(term)
        if index >= 0:
            located.append((index, -len(term), term))
    for _index, _length, term in sorted(located):
        if add(term):
            return terms

    # 新领域词（例如“灰度”）不在词典里时，先剔除已识别的业务意图和口语，
    # 再从剩余片段补召回，避免“度发布”“署文档”这类跨词噪声抢占名额。
    for block in _CJK_RE.findall(text):
        cleaned = block
        for phrase in sorted(_FILLER_PHRASES, key=len, reverse=True):
            cleaned = cleaned.replace(phrase, "")
        residual = cleaned
        for term in sorted(_INTENT_TERMS, key=len, reverse=True):
            residual = residual.replace(term, " ")
        for segment in _CJK_RE.findall(residual):
            if len(segment) < 2:
                continue
            if len(segment) <= 6 and add(segment):
                return terms
            for index in range(len(segment) - 1):
                if add(segment[index:index + 2]):
                    return terms
    return terms


def infer_query_intent(question):
    """从自然语言问题提取产品、动作、文档、信息字段和“最新”要求。"""
    terms = key_terms(question, limit=12)
    information_terms = tuple(item for item in terms if item in _INFORMATION_TERMS)
    product_terms = tuple(
        item
        for item in terms
        if re.search(r"[a-z0-9]", item, re.IGNORECASE)
        and item not in information_terms
    )
    document_terms = tuple(item for item in terms if item in _DOCUMENT_TERMS)
    action_terms = tuple(
        item
        for item in terms
        if item not in product_terms
        and item not in document_terms
        and item not in information_terms
    )
    wants_reference = bool(information_terms) or bool(
        re.search(r"信息|汇总|清单|一览|概览", str(question or ""), re.IGNORECASE)
    )
    wants_latest = bool(re.search(r"最新|最新版|最新版本|latest|新版", str(question or ""), re.IGNORECASE))
    return QueryIntent(
        product_terms,
        action_terms,
        document_terms,
        information_terms,
        wants_reference,
        wants_latest,
    )


class EmbeddingClient:
    """OpenAI 兼容的 /embeddings 客户端；配置为空时优雅退化为纯关键词检索。"""

    def __init__(self, model="", base_url="", api_key="", timeout_seconds=30):
        self.model = str(model or "").strip()
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.timeout_seconds = max(5, int(timeout_seconds or 30))

    @classmethod
    def from_env(cls, model):
        """从现有 OpenAI 中转配置构造，避免新增一套独立凭据。"""
        return cls(
            model=model,
            base_url=os.getenv("KNOWLEDGE_EMBEDDING_URL") or os.getenv("OPENAI_URL", ""),
            api_key=os.getenv("KNOWLEDGE_EMBEDDING_API_KEY") or os.getenv("OPENAI_KEY", ""),
        )

    @property
    def enabled(self):
        """三项配置齐备才启用向量召回。"""
        return bool(self.model and self.base_url and self.api_key)

    def embed(self, texts):
        """批量请求向量；失败时返回 None，由调用方回退关键词检索。"""
        values = [str(item or "")[:6000] for item in texts or ()]
        if not values or not self.enabled:
            return None
        url = self.base_url if self.base_url.endswith("/embeddings") else self.base_url + "/embeddings"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            response = httpx.post(
                url,
                headers=headers,
                json={"model": self.model, "input": values},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            rows = sorted(payload.get("data") or [], key=lambda item: int(item.get("index") or 0))
            vectors = [item.get("embedding") for item in rows]
            if len(vectors) != len(values) or any(not isinstance(item, list) for item in vectors):
                return None
            return vectors
        except Exception as exc:  # noqa: BLE001 - 向量不可用不能阻断关键词检索
            logger.warning("生成知识库向量失败，已回退关键词检索：%s", exc)
            return None


def _embed_chunks(chunks, knowledge_settings):
    """为切片补充向量；失败时所有切片保持无语义向量。"""
    client = EmbeddingClient.from_env(knowledge_settings.embedding_model)
    if not client.enabled:
        return []
    vectors = []
    batch_size = max(1, int(knowledge_settings.embedding_batch_size or 16))
    for start in range(0, len(chunks), batch_size):
        rows = client.embed([f"{item.get('heading_path', '')}\n{item.get('content', '')}" for item in chunks[start:start + batch_size]])
        if rows is None:
            return []
        vectors.extend(rows)
    return vectors if len(vectors) == len(chunks) else []


def _page_labels_json(page):
    """把 Confluence 标签保存为稳定 JSON 数组。"""
    return json.dumps(list(page.labels or ()), ensure_ascii=False, separators=(",", ":"))


def upsert_page(page, run_id, knowledge_settings):
    """幂等更新页面与切片，返回页面是否发生内容变化。"""
    document = KnowledgeDocument.query.filter_by(source_type="confluence", source_id=page.page_id).first()
    changed = document is None or document.content_hash != page.content_hash
    now = datetime.utcnow()
    if document is None:
        document = KnowledgeDocument(source_type="confluence", source_id=page.page_id)
        db.session.add(document)
    document.space_key = page.space_key or None
    document.title = page.title or f"Confluence {page.page_id}"
    document.url = page.url or None
    document.current_version = page.version or None
    document.updated_at_source = page.updated_at
    document.labels = _page_labels_json(page)
    document.content = page.content
    document.content_hash = page.content_hash
    document.status = "active"
    document.last_sync_run_id = run_id
    document.last_synced_at = now
    if document.id is not None and changed:
        KnowledgeChunk.query.filter_by(document_id=document.id).delete(synchronize_session=False)
    db.session.flush()

    if changed or not KnowledgeChunk.query.filter_by(document_id=document.id).first():
        chunks = chunk_text(
            page.content,
            max_chars=knowledge_settings.max_chunk_chars,
            overlap_chars=knowledge_settings.chunk_overlap_chars,
        )
        vectors = _embed_chunks(chunks, knowledge_settings)
        KnowledgeChunk.query.filter_by(document_id=document.id).delete(synchronize_session=False)
        for index, item in enumerate(chunks):
            text = item.get("content", "").strip()
            if not text:
                continue
            vector = vectors[index] if index < len(vectors) else None
            db.session.add(
                KnowledgeChunk(
                    document_id=document.id,
                    chunk_index=index,
                    heading_path=item.get("heading_path") or None,
                    content=text,
                    token_count=len(tokenize(text)),
                    content_hash=content_hash(text),
                    embedding=json.dumps(vector, separators=(",", ":")) if vector else None,
                    embedding_model=knowledge_settings.embedding_model if vector else None,
                )
            )
    return changed


def sync_confluence(settings, trigger_source="manual"):
    """同步配置范围中的 Confluence 页面，返回可审计的批次摘要。"""
    if not settings.knowledge.enabled:
        return {"ok": False, "error": "知识库未启用"}
    if not settings.confluence.configured:
        return {"ok": False, "error": "Confluence 地址或凭据未配置"}

    run_id = f"KSR-{uuid.uuid4().hex[:20].upper()}"
    run = KnowledgeSyncRun(run_id=run_id, trigger_source=str(trigger_source or "manual")[:32])
    db.session.add(run)
    db.session.commit()
    seen_ids = set()
    changed_count = 0
    complete = False
    try:
        with ConfluenceClient(settings.confluence) as client:
            cql = build_scope_cql(
                settings.knowledge.space_keys,
                settings.knowledge.labels,
                settings.knowledge.root_page_ids,
            )
            for page in client.iter_pages(cql, limit=settings.knowledge.sync_page_limit):
                seen_ids.add(page.page_id)
                changed_count += int(upsert_page(page, run_id, settings.knowledge))
                if len(seen_ids) % 20 == 0:
                    db.session.commit()
            complete = len(seen_ids) < int(settings.knowledge.sync_page_limit or 5000)

        deleted = 0
        if complete:
            stale = KnowledgeDocument.query.filter(KnowledgeDocument.source_type == "confluence").all()
            for document in stale:
                if document.source_id in seen_ids or document.status == "deleted":
                    continue
                document.status = "deleted"
                deleted += 1

        run.status = "succeeded"
        run.pages_seen = len(seen_ids)
        run.pages_changed = changed_count
        run.pages_deleted = deleted
        run.finished_at = datetime.utcnow()
        db.session.commit()
        return {
            "ok": True,
            "run_id": run_id,
            "pages_seen": run.pages_seen,
            "pages_changed": run.pages_changed,
            "pages_deleted": run.pages_deleted,
            "complete": complete,
        }
    except Exception as exc:  # noqa: BLE001 - 批次失败必须落库
        db.session.rollback()
        run = KnowledgeSyncRun.query.filter_by(run_id=run_id).first()
        if run is not None:
            run.status = "failed"
            run.error_message = str(exc)[:4000]
            run.finished_at = datetime.utcnow()
            db.session.commit()
        logger.exception("Confluence 同步失败：run_id=%s", run_id)
        return {"ok": False, "run_id": run_id, "error": str(exc)}


def _page_with_attachment_text(client, page, settings):
    """把可分析附件并入页面正文，供回答阶段读取真实文档内容。"""
    sections = []
    supported = {".docx", ".txt", ".md", ".csv", ".json", ".xml", ".log", ".yaml", ".yml"}
    max_bytes = max(1024 * 1024, int(getattr(settings.knowledge, "attachment_max_bytes", 20 * 1024 * 1024)))
    max_chars = max(1000, int(getattr(settings.knowledge, "attachment_max_chars", 120000)))
    for attachment in client.list_attachments(page.page_id):
        if os.path.splitext(attachment.title.lower())[1] not in supported:
            continue
        try:
            data = client.download_attachment(attachment, max_bytes=max_bytes)
            text = extract_attachment_text(attachment.title, data, max_chars=max_chars)
        except (ConfluenceError, OSError, ValueError, zipfile.BadZipFile) as exc:
            logger.warning("Confluence 附件读取失败：%s %s", attachment.title, exc)
            continue
        if text:
            sections.append(f"## 附件：{attachment.title}\n\n{text}")
    if not sections:
        return page
    content = (page.content.rstrip() + "\n\n" + "\n\n".join(sections)).strip()
    return replace(page, content=content)


def sync_page(page_id, settings, trigger_source="question_refresh", include_attachments=False):
    """同步单个页面，用于问答前刷新或 webhook 增量更新。"""
    if not settings.confluence.configured:
        raise ConfluenceError("Confluence 地址或凭据未配置")
    run_id = f"KSP-{uuid.uuid4().hex[:20].upper()}"
    with ConfluenceClient(settings.confluence) as client:
        page = client.get_page(page_id)
        if include_attachments:
            page = _page_with_attachment_text(client, page, settings)
    changed = upsert_page(page, run_id, settings.knowledge)
    db.session.commit()
    return {"ok": True, "page_id": page.page_id, "changed": changed, "version": page.version}


def _cosine(left, right):
    """计算两个向量的余弦相似度，空值返回 0。"""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(a) ** 2 for a in left))
    right_norm = math.sqrt(sum(float(b) ** 2 for b in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def retrieve_hits(question, project, settings, document_ids=None, limit=None):
    """混合关键词、项目元数据和可选向量，返回去重后的 Top 页面切片。"""
    max_results = max(1, int(limit or settings.knowledge.max_chunks))
    query = (
        db.session.query(KnowledgeChunk, KnowledgeDocument)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .filter(KnowledgeDocument.status == "active")
    )
    # 在线补查可能来自配置空间之外；只要调用方给出了本轮文档 ID，就不再
    # 用全量同步范围把正确候选过滤掉。普通本地检索仍遵守配置范围。
    if settings.knowledge.space_keys and not document_ids:
        query = query.filter(KnowledgeDocument.space_key.in_(settings.knowledge.space_keys))
    if document_ids:
        query = query.filter(KnowledgeDocument.id.in_([int(item) for item in document_ids]))
    rows = query.limit(5000).all()
    if not rows:
        return []

    intent = infer_query_intent(question)
    search_terms = list(intent.all_terms)
    query_tokens = []
    for term in search_terms:
        query_tokens.extend(tokenize(term))
    if not query_tokens:
        query_tokens = tokenize(question)
    query_set = set(query_tokens)
    phrase = str(question or "").strip().lower()
    product_terms = [item.lower() for item in intent.product_terms]
    intent_terms = [
        item.lower() for item in intent.action_terms + intent.document_terms if len(item) >= 2
    ]
    action_terms = [item.lower() for item in intent.action_terms]
    information_terms = [item.lower() for item in intent.information_terms]
    asks_for_credentials = bool(
        re.search(r"登录信息|登录账密|登录账号|登录密码|用户名|凭据|账号|密码", question, re.IGNORECASE)
    )
    doc_count = max(1, len({document.id for _, document in rows}))
    document_frequency = {}
    tokenized = []
    for chunk, document in rows:
        heading = chunk.heading_path or ""
        tokens = tokenize(f"{document.title}\n{heading}\n{chunk.content}")
        tokenized.append(set(tokens))
        for token in set(tokens):
            document_frequency[token] = document_frequency.get(token, 0) + 1

    project_tokens = set(tokenize(f"{project.name if project else ''}"))
    embedding_client = EmbeddingClient.from_env(settings.knowledge.embedding_model)
    query_vector = None
    if embedding_client.enabled:
        vectors = embedding_client.embed([question])
        query_vector = vectors[0] if vectors else None

    scored = []
    avg_length = max(1.0, sum(len(tokens) or 1 for tokens in tokenized) / len(tokenized))
    for (chunk, document), tokens in zip(rows, tokenized):
        token_count = max(1, len(tokens))
        lexical = 0.0
        for token in query_set:
            frequency = 1 if token in tokens else 0
            if not frequency:
                continue
            idf = math.log((doc_count + 1) / (document_frequency.get(token, 0) + 0.5))
            denominator = frequency + 1.2 * (0.25 + 0.75 * token_count / avg_length)
            lexical += idf * (frequency * 2.2) / denominator
        haystack = f"{document.title}\n{chunk.heading_path or ''}\n{chunk.content}".lower()
        if phrase and len(phrase) >= 4 and phrase in haystack:
            lexical += 4.0
        title = str(document.title or "").lower()
        heading = str(chunk.heading_path or "").lower()
        relevance_boost = 0.0

        def contains(term, value, product=False):
            if not product:
                return term in value
            # 产品名按独立前缀匹配：call 可命中 call3.x/call3.1，但不能命中
            # recall、yicall；中文产品名仍按普通包含匹配。
            if re.search(r"[a-z0-9]", term, re.IGNORECASE):
                pattern = rf"(?<![a-z0-9]){re.escape(term)}(?:\d+(?:\.(?:\d+|[a-z]))*)?(?![a-z])"
                return bool(re.search(pattern, value))
            pattern = rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])"
            return bool(re.search(pattern, value))

        for term in search_terms:
            is_product = term in product_terms
            if contains(term, title, is_product):
                relevance_boost += 9.0 if is_product else 3.0
            elif contains(term, heading, is_product):
                relevance_boost += 4.0 if is_product else 1.5
            elif contains(term, haystack, is_product):
                relevance_boost += 1.0 if is_product else 0.5
        if action_terms and not any(contains(term, haystack) for term in action_terms):
            relevance_boost -= 5.0
        if intent.document_terms and not any(contains(term, haystack) for term in intent.document_terms):
            relevance_boost -= 2.0
        if information_terms and not any(contains(term, haystack) for term in information_terms):
            relevance_boost -= 4.0
        matched_products = [
            term for term in product_terms if contains(term, haystack, True)
        ]
        if product_terms and not matched_products:
            relevance_boost -= 2.0
        elif len(product_terms) > 1:
            if len(matched_products) == len(product_terms):
                relevance_boost += 5.0
            else:
                relevance_boost -= 3.0
        title_has_product = any(contains(term, title, True) for term in product_terms)
        title_has_action = any(contains(term, title) for term in action_terms)
        title_has_document = bool(re.search(r"文档|指引|手册|操作说明|部署指南", title, re.IGNORECASE))
        title_has_reference = bool(re.search(r"信息汇总|环境信息|信息清单|配置清单|资源清单|一览|概览", title, re.IGNORECASE))
        if intent.wants_reference:
            if title_has_reference:
                relevance_boost += 12.0
            if re.search(
                r"\|\s*(环境|服务器节点名称|账号|用户名|登录方式|域名|namespace)\s*\|",
                haystack,
                re.IGNORECASE,
            ):
                # 表格续片经常从中间某一行开始，不能让它压过带字段表头的完整片段。
                relevance_boost += 10.0
            if information_terms:
                for term in information_terms:
                    if contains(term, title):
                        relevance_boost += 8.0
                        break
                    if contains(term, heading):
                        relevance_boost += 5.0
                        break
                    if contains(term, haystack):
                        relevance_boost += 4.0
                        break
            if asks_for_credentials and re.search(
                r"登录账密|登录账号|用户名|账号\s*[:：/]|密码\s*[:：/]", haystack, re.IGNORECASE
            ):
                relevance_boost += 10.0
            # “环境信息/登录信息”问的是当前参考信息，优先汇总页，
            # 避免历史故障处理记录仅因标题含产品和环境而压过现行资料。
            if re.search(r"无法访问|报错|失败|异常|故障|问题|原因|排查|处理|修复|错误", title, re.IGNORECASE):
                relevance_boost -= 12.0
            if re.search(r"旧版|旧|历史|过期|已归档|废弃|停止维护", title, re.IGNORECASE):
                relevance_boost -= 20.0
            updated_at = document.updated_at_source
            if updated_at:
                age_days = max(0, (datetime.utcnow() - updated_at).days)
                relevance_boost += max(-1.0, 6.0 - age_days / 365.0)
        if intent.wants_latest and title_has_product:
            if title_has_action:
                relevance_boost += 12.0
            else:
                relevance_boost -= 12.0
            if title_has_document:
                relevance_boost += 8.0
            else:
                relevance_boost -= 6.0
            if title_has_action and title_has_document:
                relevance_boost += 10.0
            updated_at = document.updated_at_source
            if updated_at:
                age_days = max(0, (datetime.utcnow() - updated_at).days)
                # “最新”首先看实际更新时间，而不是标题里的旧版本号。
                relevance_boost += max(-4.0, 14.0 - age_days / 180.0)
            if re.search(r"最新文档|最新版本|最新版|当前有效|现行有效", haystack, re.IGNORECASE):
                relevance_boost += 12.0
            if re.search(r"停止维护|已归档|不再支持|废弃|历史文档", haystack, re.IGNORECASE):
                relevance_boost -= 12.0
        metadata = 0.0
        if project_tokens and project_tokens.intersection(tokens):
            metadata += 0.5
        vector = 0.0
        if query_vector and chunk.embedding:
            try:
                vector = max(0.0, _cosine(query_vector, json.loads(chunk.embedding)))
            except (TypeError, ValueError, json.JSONDecodeError):
                vector = 0.0
        score = lexical * 0.75 + vector * 0.25 + metadata + relevance_boost
        if score > 0:
            scored.append(KnowledgeHit(document, chunk, score, lexical, vector))

    scored.sort(key=lambda item: (-item.score, item.document.id, item.chunk.chunk_index))
    selected = []
    per_document = {}
    for hit in scored:
        count = per_document.get(hit.document.id, 0)
        if count >= 2:
            continue
        selected.append(hit)
        per_document[hit.document.id] = count + 1
        if len(selected) >= max_results:
            break
    return [hit for hit in selected if hit.score >= float(settings.knowledge.min_score or 0)]


def refresh_hits_live(hits, settings):
    """问答前刷新命中页面，确保答案不依赖过期索引。"""
    document_ids = []
    seen = set()
    for hit in hits:
        if hit.document.source_id in seen:
            continue
        seen.add(hit.document.source_id)
        try:
            sync_page(
                hit.document.source_id,
                settings,
                trigger_source="question_refresh",
                include_attachments=True,
            )
            document_ids.append(hit.document.id)
        except Exception as exc:  # noqa: BLE001 - 单页失败时保留索引快照并记录
            logger.warning("问答前刷新 Confluence 页面失败：%s %s", hit.document.source_id, exc)
            document_ids.append(hit.document.id)
    return document_ids


def direct_confluence_search(question, settings):
    """在线搜索并落库，返回本地文档 ID 供同一轮相关性重排。"""
    intent = infer_query_intent(question)
    online_spaces = getattr(settings.knowledge, "online_search_space_keys", ())
    queries = build_question_cqls(
        question,
        intent.all_terms,
        online_spaces,
        settings.knowledge.labels,
        settings.knowledge.root_page_ids,
    )
    candidate_limit = max(20, int(getattr(settings.knowledge, "online_candidate_limit", 100)))
    run_id = f"KSQ-{uuid.uuid4().hex[:20].upper()}"
    imported = []
    seen_pages = set()
    with ConfluenceClient(settings.confluence) as client:
        for index, cql in enumerate(queries):
            if index >= 2 and seen_pages:
                break
            remaining = candidate_limit - len(seen_pages)
            if remaining <= 0:
                break
            for page in client.iter_pages(cql, limit=remaining):
                if page.page_id in seen_pages:
                    continue
                seen_pages.add(page.page_id)
                upsert_page(page, run_id, settings.knowledge)
                db.session.flush()
                document = KnowledgeDocument.query.filter_by(
                    source_type="confluence",
                    source_id=page.page_id,
                ).first()
                if document is not None and document.id not in imported:
                    imported.append(document.id)
    db.session.commit()
    return imported


def knowledge_status():
    """返回同步覆盖、最近批次和索引规模的只读摘要。"""
    latest = KnowledgeSyncRun.query.order_by(KnowledgeSyncRun.id.desc()).first()
    return {
        "documents": KnowledgeDocument.query.filter_by(status="active").count(),
        "deleted_documents": KnowledgeDocument.query.filter_by(status="deleted").count(),
        "chunks": KnowledgeChunk.query.count(),
        "latest_sync": latest.run_id if latest else None,
        "latest_sync_status": latest.status if latest else None,
        "latest_sync_finished_at": latest.finished_at.isoformat() if latest and latest.finished_at else None,
    }
