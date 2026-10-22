"""Immutable plan versions, three-gate reviews and execution admissions.

Revision ID: 0014
Revises: 0013
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("agent_plan_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("writer", sa.String(16), nullable=False),
        sa.Column("proposal", JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("intent", JSONB(), nullable=False),
        sa.Column("bindings", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_agent_plan_versions_run_id", "agent_plan_versions", ["run_id"])
    op.create_index("uq_agent_plan_version", "agent_plan_versions", ["run_id", "version"], unique=True)
    op.create_table("agent_plan_reviews",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("plan_id", sa.Uuid(), sa.ForeignKey("agent_plan_versions.id"), nullable=False, unique=True),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("agent_execution_admissions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("review_id", sa.Uuid(), sa.ForeignKey("agent_plan_reviews.id"), nullable=False, unique=True),
        sa.Column("plan_id", sa.Uuid(), sa.ForeignKey("agent_plan_versions.id"), nullable=False, unique=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.execute("""CREATE FUNCTION agent_admission_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Agent admission records are immutable'; END $$""")
    for table in ("agent_plan_versions", "agent_plan_reviews", "agent_execution_admissions"):
        op.execute(f"CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION agent_admission_immutable()")


def downgrade():
    # Never silently remove enforcement while paid work can still progress.
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM agent_runs WHERE phase = 'media'
        AND status IN ('queued', 'running', 'awaiting_confirm', 'canceling', 'needs_attention'))
        THEN RAISE EXCEPTION 'Stop and reconcile active media runs before downgrade'; END IF; END $$""")
    for table in ("agent_execution_admissions", "agent_plan_reviews", "agent_plan_versions"):
        op.drop_table(table)
    op.execute("DROP FUNCTION agent_admission_immutable()")
