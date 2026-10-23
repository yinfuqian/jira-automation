"""Create skill_call_records for per-skill execution statistics."""

from alembic import op
import sqlalchemy as sa


revision = "c4f1a7b9d2e6"
down_revision = "b7e4c2a9f310"
branch_labels = None
depends_on = None


def upgrade():
    # 只创建统计表与索引，不改动原 skill_run_records 表。
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("skill_call_records"):
        op.create_table(
            "skill_call_records",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("task_id", sa.String(length=64), nullable=False),
            sa.Column("record_id", sa.Integer(), nullable=True),
            sa.Column("skill_id", sa.String(length=64), nullable=False),
            sa.Column("skill_version", sa.String(length=64), nullable=True),
            sa.Column("issue_key", sa.String(length=64), nullable=True),
            sa.Column("jira_url", sa.String(length=500), nullable=True),
            sa.Column("trigger_source", sa.String(length=32), nullable=False),
            sa.Column("requested_by", sa.String(length=255), nullable=True),
            sa.Column("execution_status", sa.String(length=32), nullable=False),
            sa.Column("outcome", sa.String(length=16), nullable=False),
            sa.Column("conclusion_text", sa.String(length=128), nullable=True),
            sa.Column("conclusion_source", sa.String(length=16), nullable=False),
            sa.Column("failure_kind", sa.String(length=32), nullable=True),
            sa.Column("blocking_count", sa.Integer(), nullable=True),
            sa.Column("summary", sa.String(length=500), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("task_id", name="uq_skill_call_task"),
            sa.UniqueConstraint("record_id", name="uq_skill_call_record"),
        )
        existing_indexes = set()
    else:
        existing_indexes = {item["name"] for item in inspector.get_indexes("skill_call_records")}
    indexes = {
        "ix_skill_call_skill_created": ["skill_id", "created_at"],
        "ix_skill_call_status_created": ["execution_status", "created_at"],
        "ix_skill_call_outcome_created": ["outcome", "created_at"],
        "ix_skill_call_issue_created": ["issue_key", "created_at"],
        "ix_skill_call_source_created": ["conclusion_source", "created_at"],
    }
    for index_name, columns in indexes.items():
        if index_name not in existing_indexes:
            op.create_index(index_name, "skill_call_records", columns)


def downgrade():
    # 只删除本迁移新增的表及其索引。
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("skill_call_records"):
        op.drop_table("skill_call_records")
