"""Canvas Agent sessions, fenced runs, tool receipts and ordered events.

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None
JSON = postgresql.JSONB()


def upgrade():
    op.create_table("agent_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("documents.id", ondelete="SET NULL")),
        sa.Column("source_document_id", sa.Uuid(), nullable=False),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("uq_agent_session_document", "agent_sessions", ["workspace_id", "source_document_id"], unique=True)
    op.create_table("agent_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("agent_sessions.id"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("client_request_id", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("principal", sa.String(200), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("reply", sa.Text()),
        sa.Column("error", JSON),
        sa.Column("route", JSON, nullable=False),
        sa.Column("snapshot", JSON, nullable=False),
        sa.Column("assets", JSON, nullable=False),
        sa.Column("history", JSON, nullable=False),
        sa.Column("model_call_count", sa.Integer(), nullable=False),
        sa.Column("tool_call_count", sa.Integer(), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("execution_epoch", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("dispatched_until", sa.DateTime(timezone=True)),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("stop_requested_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_agent_runs_session_id", "agent_runs", ["session_id"])
    op.create_index("uq_agent_run_request", "agent_runs", ["session_id", "client_request_id"], unique=True)
    op.create_index("uq_agent_run_active", "agent_runs", ["session_id"], unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"))
    op.create_index("ix_agent_runs_due", "agent_runs", ["status", "next_run_at", "lease_until"])
    op.create_table("agent_model_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("execution_epoch", sa.Integer(), nullable=False),
        sa.Column("messages", JSON, nullable=False),
        sa.Column("response", JSON),
        sa.Column("raw_response", JSON),
        sa.Column("usage", JSON, nullable=False),
        sa.Column("request_id", sa.String(256)),
        sa.Column("error", JSON),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)))
    op.create_index("uq_agent_model_ordinal", "agent_model_calls", ["run_id", "ordinal"], unique=True)
    op.create_table("agent_tool_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("model_call_id", sa.Uuid(), sa.ForeignKey("agent_model_calls.id"), nullable=False),
        sa.Column("tool_call_id", sa.String(256), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("arguments", JSON, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("execution_epoch", sa.Integer()),
        sa.Column("output", JSON),
        sa.Column("error", JSON),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)))
    op.create_index("ix_agent_tool_calls_run_id", "agent_tool_calls", ["run_id"])
    op.create_index("uq_agent_tool_call", "agent_tool_calls", ["model_call_id", "tool_call_id"], unique=True)
    op.create_table("agent_events",
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("agent_sessions.id"), primary_key=True),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("payload", JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_agent_events_run_id", "agent_events", ["run_id"])


def downgrade():
    for table in ("agent_events", "agent_tool_calls", "agent_model_calls", "agent_runs", "agent_sessions"):
        op.drop_table(table)
