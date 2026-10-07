"""Agent harness stages: plan, steps, reflections, waiting state.

Revision ID: 0012
Revises: 0011
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None
JSON = postgresql.JSONB()


def upgrade():
    op.create_table("agent_steps",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(32), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(80), nullable=False),
        sa.Column("intent", sa.Text(), nullable=False),
        sa.Column("allowed_tools", JSON, nullable=False),
        sa.Column("depends_on", JSON, nullable=False),
        sa.Column("done_when", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("evidence_refs", JSON, nullable=False),
        sa.Column("retries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_agent_steps_run_id", "agent_steps", ["run_id"])
    op.create_index("uq_agent_step_key", "agent_steps", ["run_id", "plan_version", "key"], unique=True)
    op.create_table("agent_reflections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("agent_sessions.id"), nullable=False),
        sa.Column("model_call_id", sa.Uuid(), sa.ForeignKey("agent_model_calls.id")),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("hard_checks", JSON),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("issues", JSON, nullable=False),
        sa.Column("question", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_agent_reflections_run_id", "agent_reflections", ["run_id"])
    op.create_index("ix_agent_reflections_session_id", "agent_reflections", ["session_id"])
    op.add_column("agent_runs", sa.Column("stage", sa.String(16), nullable=False, server_default="plan"))
    op.add_column("agent_runs", sa.Column("plan", JSON))
    op.add_column("agent_runs", sa.Column("waiting", JSON))
    op.add_column("agent_model_calls", sa.Column("stage", sa.String(16)))
    op.add_column("agent_model_calls", sa.Column("step_id", sa.Uuid(), sa.ForeignKey("agent_steps.id")))
    op.add_column("agent_tool_calls", sa.Column("step_id", sa.Uuid(), sa.ForeignKey("agent_steps.id")))
    # Runs that finished before stages existed carry no stage history.
    op.execute("UPDATE agent_runs SET stage = 'done' WHERE status NOT IN ('queued', 'running')")


def downgrade():
    op.drop_column("agent_tool_calls", "step_id")
    op.drop_column("agent_model_calls", "step_id")
    op.drop_column("agent_model_calls", "stage")
    op.drop_column("agent_runs", "waiting")
    op.drop_column("agent_runs", "plan")
    op.drop_column("agent_runs", "stage")
    op.drop_table("agent_reflections")
    op.drop_table("agent_steps")
