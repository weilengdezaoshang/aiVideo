"""Batch quota, dispatch receipts, and failed-item retry links.

Revision ID: 0027
Revises: 0026
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "core_batch_quotas",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("quota_key", sa.String(64), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=True),
        sa.Column("inflight", sa.Integer(), server_default="0", nullable=False),
        sa.Column("limit_value", sa.Integer(), nullable=False),
        sa.CheckConstraint("inflight >= 0 AND limit_value > 0 AND inflight <= limit_value"),
        sa.CheckConstraint("quota_key <> ''"),
        sa.UniqueConstraint("quota_key"),
    )
    op.create_table(
        "core_batch_dispatches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("core_runs.id"), nullable=False),
        sa.Column("task_key", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("credits", sa.BigInteger(), server_default="0", nullable=False),
        sa.CheckConstraint("state IN ('reserved','submitted','settled','rejected_budget')"),
        sa.CheckConstraint("credits >= 0"),
        sa.UniqueConstraint("run_id", "task_key"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_core_batch_dispatch_open", "core_batch_dispatches", ["workspace_id", "state"])
    op.create_index("ix_core_batch_dispatch_run", "core_batch_dispatches", ["run_id", "state"])
    op.create_table(
        "core_batch_retries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("parent_run_id", sa.Uuid(), sa.ForeignKey("core_runs.id"), nullable=False),
        sa.Column("child_run_id", sa.Uuid(), nullable=True),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("quote", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("document", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("quote >= 0"),
        sa.UniqueConstraint("parent_run_id", "request_key"),
        sa.UniqueConstraint("child_run_id"),
        sa.ForeignKeyConstraint(["child_run_id", "workspace_id"], ["core_runs.id", "core_runs.workspace_id"]),
        sa.ForeignKeyConstraint(["parent_run_id", "workspace_id"], ["core_runs.id", "core_runs.workspace_id"]),
    )
    op.create_index("ix_core_batch_retry_parent", "core_batch_retries", ["parent_run_id"])
    op.create_index("ix_core_batch_retry_pending", "core_batch_retries", ["created_at", "id"],
                    postgresql_where=sa.text("child_run_id IS NOT NULL AND COALESCE((document ->> 'settled')::boolean, false) = false"))


def downgrade():
    op.drop_index("ix_core_batch_retry_pending", table_name="core_batch_retries")
    op.drop_index("ix_core_batch_retry_parent", table_name="core_batch_retries")
    op.drop_table("core_batch_retries")
    op.drop_index("ix_core_batch_dispatch_run", table_name="core_batch_dispatches")
    op.drop_index("ix_core_batch_dispatch_open", table_name="core_batch_dispatches")
    op.drop_table("core_batch_dispatches")
    op.drop_table("core_batch_quotas")
