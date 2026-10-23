"""Raw planner model responses, independent of agent_runs.
Revision ID: 0022
Revises: 0021
"""
from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "core_planner_model_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("invocation_id", sa.Uuid(), sa.ForeignKey("core_planner_invocations.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("template_version", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("tool_catalog_hash", sa.String(128), nullable=False),
        sa.Column("raw_response", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("usage", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("invocation_id"),
        sa.CheckConstraint("status IN ('reserved','sending','stored','unknown','converted')"),
    )


def downgrade():
    op.drop_table("core_planner_model_calls")
