"""创建企业微信代码问答审计表 chatbot_questions。"""

from alembic import op
import sqlalchemy as sa


revision = "e5d2a8c14b73"
down_revision = "c4f1a7b9d2e6"
branch_labels = None
depends_on = None


def upgrade():
    """创建机器人问答审计表；重复执行时保持幂等。"""
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("chatbot_questions"):
        return

    op.create_table(
        "chatbot_questions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("chatid", sa.String(length=128), nullable=True),
        sa.Column("chattype", sa.String(length=16), nullable=True),
        sa.Column("userid", sa.String(length=128), nullable=True),
        sa.Column("project_key", sa.String(length=64), nullable=True),
        sa.Column("project_name", sa.String(length=128), nullable=True),
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chatbot_questions_task_id", "chatbot_questions", ["task_id"], unique=True)
    op.create_index("ix_chatbot_questions_chatid", "chatbot_questions", ["chatid"], unique=False)
    op.create_index("ix_chatbot_questions_userid", "chatbot_questions", ["userid"], unique=False)
    op.create_index("ix_chatbot_questions_project_key", "chatbot_questions", ["project_key"], unique=False)
    op.create_index("ix_chatbot_questions_status", "chatbot_questions", ["status"], unique=False)
    op.create_index("ix_chatbot_questions_created_at", "chatbot_questions", ["created_at"], unique=False)


def downgrade():
    """删除机器人问答审计表。"""
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("chatbot_questions"):
        return
    op.drop_table("chatbot_questions")
