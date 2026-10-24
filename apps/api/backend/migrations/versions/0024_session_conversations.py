"""Session conversation intake, input revisions and durable indexes.

Revision ID: 0024
Revises: 0023
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

DUPLICATE_SESSIONS = """
SELECT workspace_id::text, canvas_id::text, user_id, count(*)
FROM core_sessions
WHERE closed_at IS NULL AND canvas_id IS NOT NULL
GROUP BY workspace_id, canvas_id, user_id
HAVING count(*) > 1
"""

DUPLICATE_RUNS = """
SELECT session_id::text, count(*)
FROM core_runs
WHERE parent_run_id IS NULL AND status NOT IN ('completed','failed','cancelled')
GROUP BY session_id
HAVING count(*) > 1
"""


def upgrade():
    conn = op.get_bind()
    dupes = list(conn.execute(sa.text(DUPLICATE_SESSIONS)))
    if dupes:
        sample = "; ".join(f"{row[0]}/{row[1]}/{row[2]}={row[3]}" for row in dupes[:8])
        raise RuntimeError(
            "core_sessions 存在重复活动会话，拒绝静默合并。请离线整理后再升级。"
            f" 样例: {sample}")
    run_dupes = list(conn.execute(sa.text(DUPLICATE_RUNS)))
    if run_dupes:
        sample = "; ".join(f"{row[0]}={row[1]}" for row in run_dupes[:8])
        raise RuntimeError(
            "core_runs 存在重复活动顶层 Run，拒绝静默合并。请离线整理后再升级。"
            f" 样例: {sample}")
    op.add_column("core_sessions", sa.Column("active_run_id", sa.Uuid(), nullable=True))
    op.add_column("core_sessions", sa.Column("input_revision", sa.Integer(), server_default="0", nullable=False))
    op.add_column("core_sessions", sa.Column("event_cursor", sa.BigInteger(), server_default="0", nullable=False))
    op.add_column("core_sessions", sa.Column("dispatch_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("core_sessions", sa.Column("dispatch_lease_owner", sa.String(128), nullable=True))
    op.add_column("core_sessions", sa.Column("dispatch_lease_until", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_core_session_dispatch", "core_sessions", ["dispatch_at"])
    op.create_index("ux_core_session_active_canvas_user", "core_sessions",
        ["workspace_id", "canvas_id", "user_id"], unique=True,
        postgresql_where=sa.text("closed_at IS NULL AND canvas_id IS NOT NULL"))
    op.add_column("core_session_messages", sa.Column("client_id", sa.String(128), nullable=True))
    op.add_column("core_session_messages", sa.Column("client_message_id", sa.String(128), nullable=True))
    op.add_column("core_session_messages", sa.Column("request_hash", sa.String(64), nullable=True))
    op.add_column("core_session_messages", sa.Column("intent", sa.String(32), nullable=True))
    op.add_column("core_session_messages", sa.Column("related_run_id", sa.Uuid(), nullable=True))
    op.add_column("core_session_messages", sa.Column("correction_of_run_id", sa.Uuid(), nullable=True))
    op.add_column("core_session_messages", sa.Column("input_revision", sa.Integer(), nullable=True))
    op.create_index("ux_core_message_client", "core_session_messages",
        ["session_id", "client_id", "client_message_id"], unique=True,
        postgresql_where=sa.text("client_message_id IS NOT NULL"))
    op.add_column("core_runs", sa.Column("input_revision", sa.Integer(), server_default="1", nullable=False))
    op.add_column("core_runs", sa.Column("first_submitted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("core_runs", sa.Column("first_submitted_task_id", sa.Uuid(), nullable=True))
    op.add_column("core_runs", sa.Column("correction_of_run_id", sa.Uuid(), nullable=True))
    op.add_column("core_runs", sa.Column("submit_fence", sa.Integer(), server_default="1", nullable=False))
    op.execute("""
        UPDATE core_runs AS r SET
            first_submitted_task_id = s.task_id,
            first_submitted_at = s.started
        FROM (
            SELECT DISTINCT ON (run_id) run_id, id AS task_id,
                COALESCE(started_at, created_at) AS started
            FROM core_tasks
            WHERE status IN ('submitted','succeeded','failed','blocked_by_model')
            ORDER BY run_id, started_at NULLS LAST, created_at, id
        ) AS s
        WHERE r.id = s.run_id AND r.first_submitted_at IS NULL
    """)
    op.execute("UPDATE core_runs SET submit_fence = input_revision")
    op.create_index("ux_core_run_active_top", "core_runs", ["session_id"], unique=True,
        postgresql_where=sa.text("parent_run_id IS NULL AND status NOT IN ('completed','failed','cancelled')"))
    op.execute("""
        UPDATE core_sessions AS s SET active_run_id = r.id, input_revision = r.input_revision
        FROM core_runs AS r
        WHERE r.session_id = s.id AND r.parent_run_id IS NULL
          AND r.status NOT IN ('completed','failed','cancelled')
    """)
    op.add_column("core_plans", sa.Column("input_revision", sa.Integer(), server_default="1", nullable=False))
    op.add_column("core_tasks", sa.Column("input_revision", sa.Integer(), server_default="1", nullable=False))
    op.create_table("core_run_input_revisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("message_ids", postgresql.JSONB(), nullable=False),
        sa.Column("effective_request", postgresql.JSONB(), nullable=False),
        sa.Column("selection_snapshot", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("user_correction", postgresql.JSONB(), nullable=True),
        sa.Column("decision_ids", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("context_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("run_id", "revision"),
        sa.UniqueConstraint("id", "run_id"),
        sa.ForeignKeyConstraint(["run_id", "session_id"], ["core_runs.id", "core_runs.session_id"]),
        sa.CheckConstraint("revision > 0"))
    op.create_table("core_session_artifacts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("core_sessions.id"), nullable=False),
        sa.Column("ordinal", sa.BigInteger(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("output_port", sa.String(64), server_default="default", nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("core_asset_id", sa.Uuid(), nullable=True),
        sa.Column("core_asset_version_id", sa.Uuid(), nullable=True),
        sa.Column("stored_asset_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("canvas_node_id", sa.String(128), nullable=True),
        sa.Column("position", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint("session_id", "ordinal"),
        sa.UniqueConstraint("task_id", "output_port"),
        sa.ForeignKeyConstraint(["run_id", "session_id"], ["core_runs.id", "core_runs.session_id"]),
        sa.CheckConstraint("ordinal > 0"))
    op.execute("""
        WITH produced AS (
            SELECT DISTINCT ON (t.id) t.id AS task_id, r.id AS run_id, r.session_id,
                v.asset_id, v.id AS version_id, v.stored_asset_id,
                COALESCE(t.finished_at, v.created_at) AS completed_at,
                w.node_id, w.rect
            FROM core_tasks t JOIN core_runs r ON r.id = t.run_id
            JOIN core_asset_versions v ON v.task_id = t.id AND v.run_id = r.id
            LEFT JOIN core_canvas_writes w ON w.task_id = t.id
            WHERE t.status IN ('succeeded','cancelled') AND v.category = 'artifact'
              AND v.stored_asset_id IS NOT NULL
            ORDER BY t.id, v.version DESC, v.id
        )
        INSERT INTO core_session_artifacts
            (id, session_id, ordinal, task_id, output_port, completed_at, core_asset_id,
             core_asset_version_id, stored_asset_id, run_id, canvas_node_id, position)
        SELECT gen_random_uuid(), session_id,
            row_number() OVER (PARTITION BY session_id ORDER BY completed_at, task_id),
            task_id, 'default', completed_at, asset_id, version_id, stored_asset_id,
            run_id, node_id, rect FROM produced
    """)
    op.create_table("core_session_decisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("core_sessions.id"), nullable=False),
        sa.Column("source_message_id", sa.Uuid(), nullable=True),
        sa.Column("confirm_task_id", sa.Uuid(), nullable=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("superseded_by_id", sa.Uuid(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.ForeignKeyConstraint(["source_message_id"], ["core_session_messages.id"]),
        sa.ForeignKeyConstraint(["superseded_by_id"], ["core_session_decisions.id"]),
        sa.CheckConstraint("version > 0"))
    op.create_table("core_session_summaries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("core_sessions.id"), nullable=False),
        sa.Column("from_sequence", sa.BigInteger(), nullable=False),
        sa.Column("to_sequence", sa.BigInteger(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("decision_ids", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("writer", sa.String(16), nullable=False),
        sa.Column("invocation_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint("from_sequence > 0 AND to_sequence >= from_sequence"),
        sa.CheckConstraint("writer IN ('model','extract')"))
    op.create_table("core_session_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("core_sessions.id"), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("causation_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("version_id", sa.Uuid(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.UniqueConstraint("session_id", "sequence"),
        sa.ForeignKeyConstraint(["message_id"], ["core_session_messages.id"]),
        sa.ForeignKeyConstraint(["run_id", "session_id"], ["core_runs.id", "core_runs.session_id"]))
    op.create_table("core_session_notifications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("core_sessions.id"), nullable=False),
        sa.Column("user_id", sa.String(200), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("body", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("event_id", "user_id"),
        sa.CheckConstraint("kind IN ('run_completed','task_slow','session_event')"))
    # active_run_id is a repairable cache; no FK lock on the active Run during intake.
    op.execute("""
        DO $$
        DECLARE cname text;
        BEGIN
          SELECT conname INTO cname FROM pg_constraint
          WHERE conrelid = 'core_tasks'::regclass AND contype = 'u'
            AND pg_get_constraintdef(oid) = 'UNIQUE (run_id, task_key)';
          IF cname IS NOT NULL THEN
            EXECUTE format('ALTER TABLE core_tasks DROP CONSTRAINT %I', cname);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'core_tasks'::regclass AND contype = 'u'
              AND pg_get_constraintdef(oid) LIKE 'UNIQUE (run_id, plan_id, task_key)'
          ) THEN
            ALTER TABLE core_tasks ADD CONSTRAINT ux_core_task_plan_key UNIQUE (run_id, plan_id, task_key);
          END IF;
        END $$;
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION core_forbid_revision_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'input revisions are append-only';
        END $$;
        CREATE TRIGGER trg_core_run_input_revisions_immutable
        BEFORE UPDATE OR DELETE ON core_run_input_revisions
        FOR EACH ROW EXECUTE FUNCTION core_forbid_revision_mutation();
        CREATE OR REPLACE FUNCTION core_forbid_decision_value_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.value IS DISTINCT FROM OLD.value THEN
            RAISE EXCEPTION 'confirmed decision values are immutable';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER trg_core_session_decisions_value
        BEFORE UPDATE ON core_session_decisions
        FOR EACH ROW EXECUTE FUNCTION core_forbid_decision_value_change();
    """)


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS trg_core_session_decisions_value ON core_session_decisions")
    op.execute("DROP TRIGGER IF EXISTS trg_core_run_input_revisions_immutable ON core_run_input_revisions")
    op.drop_table("core_session_notifications")
    op.drop_table("core_session_events")
    op.drop_table("core_session_summaries")
    op.drop_table("core_session_decisions")
    op.drop_table("core_session_artifacts")
    op.drop_table("core_run_input_revisions")
    op.drop_column("core_tasks", "input_revision")
    op.drop_column("core_plans", "input_revision")
    op.drop_index("ux_core_run_active_top", table_name="core_runs")
    op.execute("DROP FUNCTION IF EXISTS core_forbid_decision_value_change()")
    op.execute("DROP FUNCTION IF EXISTS core_forbid_revision_mutation()")
    op.drop_column("core_runs", "correction_of_run_id")
    op.drop_column("core_runs", "submit_fence")
    op.drop_column("core_runs", "first_submitted_task_id")
    op.drop_column("core_runs", "first_submitted_at")
    op.drop_column("core_runs", "input_revision")
    op.drop_index("ux_core_message_client", table_name="core_session_messages")
    op.drop_column("core_session_messages", "input_revision")
    op.drop_column("core_session_messages", "correction_of_run_id")
    op.drop_column("core_session_messages", "related_run_id")
    op.drop_column("core_session_messages", "intent")
    op.drop_column("core_session_messages", "request_hash")
    op.drop_column("core_session_messages", "client_message_id")
    op.drop_column("core_session_messages", "client_id")
    op.drop_index("ux_core_session_active_canvas_user", table_name="core_sessions")
    op.drop_index("ix_core_session_dispatch", table_name="core_sessions")
    op.drop_column("core_sessions", "dispatch_lease_until")
    op.drop_column("core_sessions", "dispatch_lease_owner")
    op.drop_column("core_sessions", "dispatch_at")
    op.drop_column("core_sessions", "event_cursor")
    op.drop_column("core_sessions", "input_revision")
    op.drop_column("core_sessions", "active_run_id")
