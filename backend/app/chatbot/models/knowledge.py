"""Confluence 知识库的持久化模型。

页面元数据、正文切片、同步批次和回答引用分开存储，避免把检索索引
耦合到机器人问答审计表。向量以 JSON 文本保存，后续可无痛替换为
OpenSearch 等专用索引。
"""
from datetime import datetime

from sqlalchemy.dialects.mysql import LONGTEXT

from extensions import db


def _long_text():
    """返回 MySQL LONGTEXT、其它数据库 TEXT 的兼容列类型。"""
    return db.Text().with_variant(LONGTEXT(), "mysql")


def _id_type():
    """MySQL 使用 BIGINT，SQLite 测试库使用可自增的 INTEGER。"""
    return db.BigInteger().with_variant(db.Integer(), "sqlite")


class KnowledgeDocument(db.Model):
    """Confluence 页面当前快照；一个远端页面只保留一条有效记录。"""

    __tablename__ = "knowledge_documents"

    id = db.Column(_id_type(), primary_key=True, autoincrement=True)
    source_type = db.Column(db.String(32), nullable=False, default="confluence", index=True)
    source_id = db.Column(db.String(128), nullable=False, unique=True, index=True)
    space_key = db.Column(db.String(64), nullable=True, index=True)
    title = db.Column(db.String(500), nullable=False)
    url = db.Column(db.String(1000), nullable=True)
    current_version = db.Column(db.Integer, nullable=True)
    updated_at_source = db.Column(db.DateTime, nullable=True)
    labels = db.Column(_long_text(), nullable=True)
    content = db.Column(_long_text(), nullable=False)
    content_hash = db.Column(db.String(64), nullable=False, index=True)
    status = db.Column(db.String(24), nullable=False, default="active", index=True)
    last_sync_run_id = db.Column(db.String(64), nullable=True, index=True)
    last_synced_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class KnowledgeChunk(db.Model):
    """页面按标题与长度切开后的检索单元。"""

    __tablename__ = "knowledge_chunks"

    id = db.Column(_id_type(), primary_key=True, autoincrement=True)
    document_id = db.Column(
        db.BigInteger,
        db.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index = db.Column(db.Integer, nullable=False)
    heading_path = db.Column(db.String(1000), nullable=True)
    content = db.Column(_long_text(), nullable=False)
    token_count = db.Column(db.Integer, nullable=False, default=0)
    content_hash = db.Column(db.String(64), nullable=False)
    embedding = db.Column(_long_text(), nullable=True)
    embedding_model = db.Column(db.String(128), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("document_id", "chunk_index", name="uq_knowledge_chunk_document_index"),
    )


class KnowledgeSyncRun(db.Model):
    """记录一次 Confluence 同步的覆盖范围和执行结果。"""

    __tablename__ = "knowledge_sync_runs"

    id = db.Column(_id_type(), primary_key=True, autoincrement=True)
    run_id = db.Column(db.String(64), nullable=False, unique=True, index=True)
    trigger_source = db.Column(db.String(32), nullable=False, default="scheduled")
    status = db.Column(db.String(24), nullable=False, default="running", index=True)
    pages_seen = db.Column(db.Integer, nullable=False, default=0)
    pages_changed = db.Column(db.Integer, nullable=False, default=0)
    pages_deleted = db.Column(db.Integer, nullable=False, default=0)
    error_message = db.Column(db.Text, nullable=True)
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime, nullable=True)


class ChatbotAnswerSource(db.Model):
    """保存一次机器人回答实际使用的 Confluence 页面和切片。"""

    __tablename__ = "chatbot_answer_sources"

    id = db.Column(_id_type(), primary_key=True, autoincrement=True)
    task_id = db.Column(db.String(64), nullable=False, index=True)
    document_id = db.Column(db.BigInteger, nullable=False, index=True)
    chunk_id = db.Column(db.BigInteger, nullable=True, index=True)
    page_version = db.Column(db.Integer, nullable=True)
    rank = db.Column(db.Integer, nullable=False, default=0)
    score = db.Column(db.Float, nullable=False, default=0.0)
    cited = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
