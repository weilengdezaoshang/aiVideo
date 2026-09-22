"""初始 Schema:工作区/文档/幂等受理/任务/尝试/资产/引用/Outbox。

Revision ID: 0001
Revises:
Create Date: 2026-09-13

人工编写并对照 backend/infrastructure/orm.py 审查;
模型漂移由 tests/backend/test_persistence.py::test_alembic_upgrade_creates_expected_schema 把关。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, server_default="默认工作区"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False, server_default="未命名画布"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("data", JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_documents_workspace_id", "documents", ["workspace_id"])
    op.create_index("ix_documents_workspace_updated", "documents", ["workspace_id", "updated_at"])
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("generation_request_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("params", JSONB(), nullable=False),
        sa.Column("progress", sa.Float(), nullable=True),
        sa.Column("message", sa.String(255), nullable=False, server_default="排队中"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(32), nullable=True),
        sa.Column("recovery", sa.String(16), nullable=True),
        sa.Column("external_task_id", sa.String(128), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("execution_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("slot_scope", sa.String(32), nullable=True),
        sa.Column("slot_acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_jobs_workspace_status_created", "jobs", ["workspace_id", "status", "created_at"])
    op.create_index("ix_jobs_status_next_poll", "jobs", ["status", "next_poll_at"])
    op.create_table(
        "generation_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("params", JSONB(), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "uq_generation_requests_scope_key",
        "generation_requests",
        ["workspace_id", "request_id"],
        unique=True,
    )
    op.create_table(
        "attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_task_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("error_code", sa.String(32), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_attempts_job_id", "attempts", ["job_id"])
    op.create_index("uq_attempts_job_no", "attempts", ["job_id", "attempt_no"], unique=True)
    op.create_table(
        "assets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("ext", sa.String(8), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("height", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("storage_key", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_assets_workspace_created", "assets", ["workspace_id", "created_at"])
    op.create_table(
        "asset_references",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("asset_id", sa.Uuid(), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("owner_type", sa.String(16), nullable=False),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_asset_references_asset_id", "asset_references", ["asset_id"])
    op.create_index(
        "uq_asset_references",
        "asset_references",
        ["asset_id", "owner_type", "owner_id"],
        unique=True,
    )
    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("aggregate_type", sa.String(16), nullable=False),
        sa.Column("aggregate_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("schema_version", sa.String(8), nullable=False, server_default="1"),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_outbox_status_next_attempt", "outbox", ["status", "next_attempt_at"])


def downgrade() -> None:
    for table in (
        "outbox",
        "asset_references",
        "assets",
        "attempts",
        "generation_requests",
        "jobs",
        "documents",
        "workspaces",
    ):
        op.drop_table(table)
