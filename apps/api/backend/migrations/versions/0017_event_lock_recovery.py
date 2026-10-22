"""Nonblocking timer scheduling and terminal event integrity records.
Revision ID: 0017
Revises: 0016
"""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

TABLE_DDL = ["\nCREATE TABLE core_event_quarantine (\n\tsource_key VARCHAR(256) NOT NULL, \n\tevent_id UUID, \n\trun_id UUID, \n\tcausation_id UUID, \n\ttimer_id UUID, \n\tcode VARCHAR(64) NOT NULL, \n\tcontext JSONB NOT NULL, \n\tresolved_at TIMESTAMP WITH TIME ZONE, \n\tid UUID NOT NULL, \n\tschema_version INTEGER DEFAULT '1' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (source_key)\n)\n\n", 'CREATE INDEX ix_core_quarantine_event ON core_event_quarantine (event_id, resolved_at)']


def upgrade():
    op.add_column("core_event_timers", sa.Column("next_fire_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE core_event_timers SET next_fire_at = due_at")
    op.alter_column("core_event_timers", "next_fire_at", nullable=False)
    op.add_column("core_event_timers", sa.Column("lock_skip_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("core_event_timers", sa.Column("claim_token", sa.Uuid()))
    op.add_column("core_event_timers", sa.Column("lease_until", sa.DateTime(timezone=True)))
    op.add_column("core_event_alerts", sa.Column("context", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"), nullable=False, server_default="{}"))
    # 0016 intentionally used anonymous checks; resolve their actual PostgreSQL names.
    op.execute("""DO $$ DECLARE c record; BEGIN
      FOR c IN SELECT conname, conrelid::regclass AS tbl FROM pg_constraint
       WHERE contype='c' AND ((conrelid='core_event_timers'::regclass AND pg_get_constraintdef(oid) LIKE '%scheduled%')
         OR (conrelid='core_event_attempts'::regclass AND pg_get_constraintdef(oid) LIKE '%abandoned%'))
      LOOP EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',c.tbl,c.conname); END LOOP;
    END $$""")
    op.create_check_constraint("ck_core_timer_status", "core_event_timers", "status IN ('scheduled','fired','cancelled','quarantined') AND version >= 0")
    op.create_check_constraint("ck_core_attempt_status", "core_event_attempts", "status IN ('running','succeeded','failed','abandoned','deferred')")
    op.create_check_constraint("ck_core_timer_schedule", "core_event_timers", "next_fire_at >= due_at AND lock_skip_count >= 0")
    op.create_index("ix_core_timer_next", "core_event_timers", ["status", "next_fire_at", "lease_until"])
    for statement in TABLE_DDL:
        op.execute(statement)


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM core_event_quarantine)
      OR EXISTS(SELECT 1 FROM core_event_timers WHERE status='quarantined' OR claim_token IS NOT NULL
        OR (status='scheduled' AND next_fire_at > due_at))
      OR EXISTS(SELECT 1 FROM core_event_attempts WHERE status='deferred') THEN
      RAISE EXCEPTION 'Reconcile event quarantine, deferred attempts and timer claims before downgrade';
    END IF; END $$""")
    op.drop_table("core_event_quarantine")
    op.drop_index("ix_core_timer_next", table_name="core_event_timers")
    op.drop_constraint("ck_core_timer_schedule", "core_event_timers")
    op.drop_constraint("ck_core_timer_status", "core_event_timers")
    op.drop_constraint("ck_core_attempt_status", "core_event_attempts")
    op.create_check_constraint("core_event_timers_check", "core_event_timers", "status IN ('scheduled','fired','cancelled') AND version >= 0")
    op.create_check_constraint("core_event_attempts_status_check", "core_event_attempts", "status IN ('running','succeeded','failed','abandoned')")
    for column in ("next_fire_at", "lock_skip_count", "claim_token", "lease_until"):
        op.drop_column("core_event_timers", column)
    op.drop_column("core_event_alerts", "context")
