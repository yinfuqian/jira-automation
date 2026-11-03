"""创建 Confluence 知识索引与问答引用表。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "f6e3b9d24c87"
down_revision = "e5d2a8c14b73"
branch_labels = None
depends_on = None


def _long_text():
    return sa.Text().with_variant(mysql.LONGTEXT(), "mysql")


def _create_index_if_missing(inspector, table_name, index_name, columns, **kwargs):
    if index_name in {item.get("name") for item in inspector.get_indexes(table_name)}:
        return
    op.create_index(index_name, table_name, columns, **kwargs)


def upgrade():
    """创建四张知识库表；重复执行时保持幂等。"""
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table("knowledge_documents"):
        op.create_table(
            "knowledge_documents",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("source_type", sa.String(length=32), nullable=False),
            sa.Column("source_id", sa.String(length=128), nullable=False),
            sa.Column("space_key", sa.String(length=64), nullable=True),
            sa.Column("title", sa.String(length=500), nullable=False),
            sa.Column("url", sa.String(length=1000), nullable=True),
            sa.Column("current_version", sa.Integer(), nullable=True),
            sa.Column("updated_at_source", sa.DateTime(), nullable=True),
            sa.Column("labels", _long_text(), nullable=True),
            sa.Column("content", _long_text(), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("last_sync_run_id", sa.String(length=64), nullable=True),
            sa.Column("last_synced_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        inspector = sa.inspect(op.get_bind())
    for index_name, columns, unique in (
        ("ix_knowledge_documents_source_type", ["source_type"], False),
        ("ix_knowledge_documents_source_id", ["source_id"], True),
        ("ix_knowledge_documents_space_key", ["space_key"], False),
        ("ix_knowledge_documents_content_hash", ["content_hash"], False),
        ("ix_knowledge_documents_status", ["status"], False),
        ("ix_knowledge_documents_last_sync_run_id", ["last_sync_run_id"], False),
    ):
        _create_index_if_missing(inspector, "knowledge_documents", index_name, columns, unique=unique)

    if not inspector.has_table("knowledge_chunks"):
        op.create_table(
            "knowledge_chunks",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("document_id", sa.BigInteger(), nullable=False),
            sa.Column("chunk_index", sa.Integer(), nullable=False),
            sa.Column("heading_path", sa.String(length=1000), nullable=True),
            sa.Column("content", _long_text(), nullable=False),
            sa.Column("token_count", sa.Integer(), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("embedding", _long_text(), nullable=True),
            sa.Column("embedding_model", sa.String(length=128), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["document_id"], ["knowledge_documents.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("document_id", "chunk_index", name="uq_knowledge_chunk_document_index"),
        )
        inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "knowledge_chunks", "ix_knowledge_chunks_document_id", ["document_id"])

    if not inspector.has_table("knowledge_sync_runs"):
        op.create_table(
            "knowledge_sync_runs",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("run_id", sa.String(length=64), nullable=False),
            sa.Column("trigger_source", sa.String(length=32), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("pages_seen", sa.Integer(), nullable=False),
            sa.Column("pages_changed", sa.Integer(), nullable=False),
            sa.Column("pages_deleted", sa.Integer(), nullable=False),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "knowledge_sync_runs", "ix_knowledge_sync_runs_run_id", ["run_id"], unique=True)
    _create_index_if_missing(inspector, "knowledge_sync_runs", "ix_knowledge_sync_runs_status", ["status"])

    if not inspector.has_table("chatbot_answer_sources"):
        op.create_table(
            "chatbot_answer_sources",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("task_id", sa.String(length=64), nullable=False),
            sa.Column("document_id", sa.BigInteger(), nullable=False),
            sa.Column("chunk_id", sa.BigInteger(), nullable=True),
            sa.Column("page_version", sa.Integer(), nullable=True),
            sa.Column("rank", sa.Integer(), nullable=False),
            sa.Column("score", sa.Float(), nullable=False),
            sa.Column("cited", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "chatbot_answer_sources", "ix_chatbot_answer_sources_task_id", ["task_id"])
    _create_index_if_missing(inspector, "chatbot_answer_sources", "ix_chatbot_answer_sources_document_id", ["document_id"])
    _create_index_if_missing(inspector, "chatbot_answer_sources", "ix_chatbot_answer_sources_chunk_id", ["chunk_id"])


def downgrade():
    """按外键依赖顺序删除知识库表。"""
    inspector = sa.inspect(op.get_bind())
    for table_name in (
        "chatbot_answer_sources",
        "knowledge_sync_runs",
        "knowledge_chunks",
        "knowledge_documents",
    ):
        if inspector.has_table(table_name):
            op.drop_table(table_name)
