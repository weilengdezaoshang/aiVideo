"""Persist media DAG execution, reservations and document node leases.

Revision ID: 0013
Revises: 0012
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None
JSON = postgresql.JSONB()


def upgrade():
    op.add_column("agent_sessions", sa.Column("principal", sa.String(200), nullable=False, server_default=""))
    op.drop_index("uq_agent_session_document", table_name="agent_sessions")
    op.create_index("uq_agent_session_document", "agent_sessions",
                    ["workspace_id", "source_document_id", "principal"], unique=True)
    op.add_column("agent_runs", sa.Column("execution", JSON))
    op.add_column("agent_steps", sa.Column("execution", JSON))
    op.drop_index("uq_agent_run_active", table_name="agent_runs")
    op.create_index("uq_agent_run_active", "agent_runs", ["session_id"], unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running') AND phase <> 'media'"))
    op.create_table("host_credit_accounts",
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("available", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint("available >= 0", name="ck_host_credit_nonnegative"))
    op.create_table("host_credit_grants",
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("request_id", sa.String(128), primary_key=True),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("principal", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("amount > 0", name="ck_host_credit_grant_positive"))
    op.create_table("host_credit_ledger",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("step_id", sa.Uuid(), sa.ForeignKey("agent_steps.id"), nullable=False),
        sa.Column("child_index", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id")),
        sa.Column("quoted", sa.BigInteger(), nullable=False),
        sa.Column("charged", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("refunded", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(24), nullable=False, server_default="reserved"),
        sa.Column("details", JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("quoted >= 0 AND charged >= 0 AND refunded >= 0 AND charged + refunded <= quoted",
                           name="ck_host_credit_conservation"))
    op.create_index("ix_host_credit_ledger_workspace_id", "host_credit_ledger", ["workspace_id"])
    op.create_index("ix_host_credit_ledger_run_id", "host_credit_ledger", ["run_id"])
    op.create_index("uq_host_credit_step_attempt", "host_credit_ledger",
                    ["run_id", "step_id", "child_index", "attempt"], unique=True)
    op.create_table("host_node_leases",
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("node_id", sa.String(200), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("step_id", sa.Uuid(), sa.ForeignKey("agent_steps.id"), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_host_node_leases_run_id", "host_node_leases", ["run_id"])


def downgrade():
    op.drop_table("host_node_leases")
    op.drop_table("host_credit_ledger")
    op.drop_table("host_credit_grants")
    op.drop_table("host_credit_accounts")
    op.drop_index("uq_agent_run_active", table_name="agent_runs")
    # Refuse unsafe rollback while more than one media run shares a session.
    op.create_index("uq_agent_run_active", "agent_runs", ["session_id"], unique=True,
                    postgresql_where=sa.text("status IN ('queued', 'running')"))
    op.drop_column("agent_steps", "execution")
    op.drop_column("agent_runs", "execution")
    # Media sessions are retained for audit; multiple user sessions prevent safe rollback.
    op.drop_index("uq_agent_session_document", table_name="agent_sessions")
    op.create_index("uq_agent_session_document", "agent_sessions", ["workspace_id", "source_document_id"], unique=True)
    op.drop_column("agent_sessions", "principal")
