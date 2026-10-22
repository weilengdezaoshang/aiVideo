"""Core event collaboration: durable inbox, retries, dead letters and timers.
Revision ID: 0016
Revises: 0015
"""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

TABLE_DDL = [
    """CREATE TABLE core_event_alerts (
	kind VARCHAR(64) NOT NULL,
	event_id UUID,
	dedupe_key VARCHAR(256) NOT NULL,
	occurrences BIGINT DEFAULT '1' NOT NULL,
	last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	resolved_at TIMESTAMP WITH TIME ZONE,
	id UUID NOT NULL,
	schema_version INTEGER DEFAULT '1' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(event_id) REFERENCES core_event_outbox (id),
	UNIQUE (dedupe_key)
)""",
    """CREATE TABLE core_event_attempts (
	event_id UUID NOT NULL,
	generation INTEGER NOT NULL,
	number INTEGER NOT NULL,
	claim_token UUID NOT NULL,
	handler_version VARCHAR(128) NOT NULL,
	status VARCHAR(16) NOT NULL,
	finished_at TIMESTAMP WITH TIME ZONE,
	error TEXT,
	id UUID NOT NULL,
	schema_version INTEGER DEFAULT '1' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (event_id, generation, number),
	CHECK (status IN ('running','succeeded','failed','abandoned')),
	FOREIGN KEY(event_id) REFERENCES core_event_outbox (id),
	UNIQUE (claim_token)
)""",
    """CREATE TABLE core_event_dead_letters (
	event_id UUID NOT NULL,
	error TEXT NOT NULL,
	handler_version VARCHAR(128),
	entered_at TIMESTAMP WITH TIME ZONE NOT NULL,
	resolved_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (event_id),
	FOREIGN KEY(event_id) REFERENCES core_event_outbox (id)
)""",
    """CREATE TABLE core_event_receipts (
	event_id UUID NOT NULL,
	run_id UUID NOT NULL,
	run_sequence BIGINT NOT NULL,
	status VARCHAR(24) DEFAULT 'pending' NOT NULL,
	generation INTEGER DEFAULT '0' NOT NULL,
	attempts INTEGER DEFAULT '0' NOT NULL,
	handler_version VARCHAR(128),
	claim_token UUID,
	lease_until TIMESTAMP WITH TIME ZONE,
	ready_at TIMESTAMP WITH TIME ZONE NOT NULL,
	processed_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (event_id),
	UNIQUE (run_id, run_sequence),
	CHECK (status IN ('pending','running','retry','waiting_handler','processed','dead')),
	CHECK (attempts >= 0 AND generation >= 0),
	FOREIGN KEY(event_id) REFERENCES core_event_outbox (id),
	FOREIGN KEY(run_id) REFERENCES core_runs (id)
)""",
    """CREATE TABLE core_event_replays (
	event_id UUID NOT NULL,
	generation INTEGER NOT NULL,
	operator VARCHAR(200) NOT NULL,
	reason TEXT NOT NULL,
	request_key VARCHAR(128) NOT NULL,
	id UUID NOT NULL,
	schema_version INTEGER DEFAULT '1' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (event_id, generation),
	FOREIGN KEY(event_id) REFERENCES core_event_outbox (id),
	UNIQUE (request_key)
)""",
    """CREATE TABLE core_event_timers (
	run_id UUID NOT NULL,
	task_id UUID,
	causation_id UUID NOT NULL,
	event_type VARCHAR(64) NOT NULL,
	idempotency_key VARCHAR(128) NOT NULL,
	due_at TIMESTAMP WITH TIME ZONE NOT NULL,
	expected_version BIGINT,
	status VARCHAR(16) DEFAULT 'scheduled' NOT NULL,
	version INTEGER DEFAULT '0' NOT NULL,
	event_id UUID,
	fired_at TIMESTAMP WITH TIME ZONE,
	id UUID NOT NULL,
	schema_version INTEGER DEFAULT '1' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (run_id, idempotency_key),
	FOREIGN KEY(task_id, run_id) REFERENCES core_tasks (id, run_id),
	CHECK (status IN ('scheduled','fired','cancelled') AND version >= 0),
	CHECK ((status = 'fired') = (event_id IS NOT NULL)),
	FOREIGN KEY(run_id) REFERENCES core_runs (id),
	FOREIGN KEY(causation_id) REFERENCES core_event_outbox (id),
	UNIQUE (event_id),
	FOREIGN KEY(event_id) REFERENCES core_event_outbox (id)
)""",
    """CREATE TABLE core_run_consumption (
	run_id UUID NOT NULL,
	completed_sequence BIGINT DEFAULT '0' NOT NULL,
	blocked_event_id UUID,
	ready_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (run_id),
	CHECK (completed_sequence >= 0),
	FOREIGN KEY(run_id) REFERENCES core_runs (id),
	FOREIGN KEY(blocked_event_id) REFERENCES core_event_outbox (id)
)""",
    """CREATE INDEX ix_core_alert_open ON core_event_alerts (kind, resolved_at)""",
    """CREATE INDEX ix_core_receipt_ready ON core_event_receipts (status, ready_at)""",
    """CREATE INDEX ix_core_timer_due ON core_event_timers (status, due_at)""",
    """CREATE INDEX ix_core_stream_ready ON core_run_consumption (ready_at)"""
]

APPLY_EVENT = """CREATE OR REPLACE FUNCTION core_apply_state_event() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE r core_runs%ROWTYPE; t core_tasks%ROWTYPE; actual_state text;
      expected bigint; valid boolean; changed integer; seq bigint; resume text;
    BEGIN

      IF NEW.event_kind = 'domain' THEN
        SELECT * INTO r FROM core_runs WHERE id = NEW.run_id FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE = 'P4004', MESSAGE = 'run not found'; END IF;
        IF NEW.event_type NOT IN ('user_message','user_interrupt','plan_written','plan_rejected',
          'task_succeeded','task_failed','task_blocked','helper_report','helper_help','helper_instruction',
          'conflict','asset_changed','timeout','budget_exceeded','user_confirmed','user_cancelled','run_paused','run_resumed')
          OR NEW.causation_id IS NULL OR NEW.causation_id = NEW.id OR NEW.aggregate_version IS NOT NULL
          OR NEW.from_state IS NOT NULL OR NEW.to_state IS NOT NULL THEN
          RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'invalid domain event'; END IF;
        IF jsonb_typeof(NEW.payload) IS DISTINCT FROM 'object' OR EXISTS (
          SELECT 1 FROM jsonb_each_text(NEW.payload) p WHERE p.key !~ '_id$'
            OR p.value IS NULL OR p.value !~* '^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$') THEN
          RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'claim check payload must contain only UUID ids'; END IF;
        NEW.aggregate_type := 'run'; NEW.aggregate_id := r.id;
        NEW.plan_id := COALESCE(NEW.plan_id, r.current_plan_id);
        SELECT version INTO NEW.plan_version FROM core_plans WHERE id = NEW.plan_id AND run_id = r.id;
        UPDATE core_runs SET event_cursor = event_cursor + 1 WHERE id = r.id RETURNING event_cursor INTO seq;
        NEW.run_sequence := seq; NEW.correlation_id := COALESCE(NEW.correlation_id, r.id);
        NEW.payload := NEW.payload || jsonb_strip_nulls(jsonb_build_object('run_id',r.id,'task_id',NEW.task_id,'plan_id',NEW.plan_id));
        RETURN NEW;
      END IF;
      IF NEW.aggregate_type = 'run' THEN
        NEW.run_id := NEW.aggregate_id; NEW.task_id := NULL;
      ELSIF NEW.aggregate_type = 'task' THEN
        SELECT * INTO t FROM core_tasks WHERE id = NEW.aggregate_id;
        IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE = 'P4004', MESSAGE = 'task not found'; END IF;
        NEW.run_id := t.run_id; NEW.task_id := t.id;
      ELSE RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'unknown aggregate'; END IF;
      -- Every event for a Run is serialized, including events from separate Tasks.
      SELECT * INTO r FROM core_runs WHERE id = NEW.run_id FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE = 'P4004', MESSAGE = 'run not found'; END IF;
      IF NEW.causation_id IS NULL AND r.event_cursor > 0 THEN
        SELECT id INTO NEW.causation_id FROM core_event_outbox WHERE run_id=r.id AND run_sequence=r.event_cursor;
      END IF;
      IF NEW.aggregate_type = 'task' THEN
        SELECT * INTO t FROM core_tasks WHERE id = NEW.aggregate_id;
        actual_state := t.status; NEW.plan_id := t.plan_id; NEW.plan_version := t.plan_version;
      ELSE
        actual_state := r.status; NEW.plan_id := r.current_plan_id;
        SELECT version INTO NEW.plan_version FROM core_plans WHERE id = r.current_plan_id;
      END IF;
      IF NEW.aggregate_version = 0 THEN
        IF pg_trigger_depth() < 2 OR NEW.from_state IS NOT NULL OR
           NEW.to_state <> (CASE WHEN NEW.aggregate_type = 'run' THEN 'created' ELSE 'pending' END) THEN
          RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'creation event belongs to record creation'; END IF;
        NEW.event_type := NEW.aggregate_type || '.created';
      ELSE
        expected := NEW.aggregate_version - 1;
        IF expected <> (CASE WHEN NEW.aggregate_type = 'run' THEN r.version ELSE t.version END) THEN
          RAISE EXCEPTION USING ERRCODE = 'P4001', MESSAGE = 'version_conflict'; END IF;
        IF NEW.from_state IS DISTINCT FROM actual_state THEN
          RAISE EXCEPTION USING ERRCODE = 'P4001', MESSAGE = 'source state changed'; END IF;
        IF NEW.aggregate_type = 'run' THEN
          valid := CASE actual_state
            WHEN 'created' THEN NEW.to_state IN ('planning','failed','cancelled')
            WHEN 'planning' THEN NEW.to_state IN ('awaiting_confirm','running','paused','failed','cancelled')
            WHEN 'awaiting_confirm' THEN NEW.to_state IN ('planning','running','paused','failed','cancelled')
            WHEN 'running' THEN NEW.to_state IN ('planning','paused','completed','failed','cancelled')
            WHEN 'paused' THEN NEW.to_state IN ('failed','cancelled') OR NEW.to_state = r.resume_state
            ELSE false END;
          IF NEW.to_state = 'paused' AND (NEW.reason IS NULL OR NEW.reason NOT IN
              ('waiting_user','over_budget','user_paused','waiting_info')) THEN valid := false; END IF;
          IF NOT COALESCE(valid, false) THEN RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'invalid Run transition'; END IF;
          UPDATE core_runs SET status = NEW.to_state, version = NEW.aggregate_version,
            pause_reason = CASE WHEN NEW.to_state = 'paused' THEN NEW.reason END,
            resume_state = CASE WHEN NEW.to_state = 'paused' THEN actual_state END,
            cancel_reason = CASE WHEN NEW.to_state = 'cancelled' THEN NEW.reason ELSE cancel_reason END,
            execution_epoch = execution_epoch + 1,
            started_at = CASE WHEN NEW.to_state = 'running' THEN COALESCE(started_at, now()) ELSE started_at END,
            finished_at = CASE WHEN NEW.to_state IN ('completed','failed','cancelled') THEN now() END,
            updated_at = now() WHERE id = r.id AND version = expected;
        ELSE
          valid := CASE actual_state
            WHEN 'pending' THEN NEW.to_state IN ('validated','rejected','cancelled')
            WHEN 'validated' THEN NEW.to_state = 'cancelled' OR
              (t.kind = 'wait_user' AND NEW.to_state = 'waiting_user') OR
              (t.kind <> 'wait_user' AND NEW.to_state = 'queued')
            WHEN 'queued' THEN NEW.to_state IN ('submitted','failed','cancelled')
            WHEN 'submitted' THEN NEW.to_state IN ('succeeded','failed','blocked_by_model','cancelled')
            WHEN 'waiting_user' THEN NEW.to_state IN ('succeeded','cancelled')
            ELSE false END;
          IF NOT COALESCE(valid, false) THEN RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'invalid Task transition'; END IF;
          UPDATE core_tasks SET status = NEW.to_state, version = NEW.aggregate_version,
            execution_epoch = execution_epoch + 1,
            started_at = CASE WHEN NEW.to_state IN ('submitted','waiting_user') THEN COALESCE(started_at, now()) ELSE started_at END,
            finished_at = CASE WHEN NEW.to_state IN ('succeeded','failed','blocked_by_model','cancelled','rejected') THEN now() END,
            updated_at = now() WHERE id = t.id AND version = expected;
        END IF;
        GET DIAGNOSTICS changed = ROW_COUNT;
        IF changed <> 1 THEN RAISE EXCEPTION USING ERRCODE = 'P4001', MESSAGE = 'version_conflict'; END IF;
        NEW.event_type := NEW.aggregate_type || '.state_changed';
      END IF;
      UPDATE core_runs SET event_cursor = event_cursor + 1 WHERE id = r.id RETURNING event_cursor INTO seq;
      NEW.run_sequence := seq;
      NEW.correlation_id := COALESCE(NEW.correlation_id, r.id);
      NEW.payload := jsonb_strip_nulls(jsonb_build_object('run_id', NEW.run_id, 'task_id', NEW.task_id, 'plan_id', NEW.plan_id));
      RETURN NEW;
    END $$"""

PREVIOUS_APPLY_EVENT = """CREATE OR REPLACE FUNCTION core_apply_state_event() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE r core_runs%ROWTYPE; t core_tasks%ROWTYPE; actual_state text;
      expected bigint; valid boolean; changed integer; seq bigint; resume text;
    BEGIN
      IF NEW.aggregate_type = 'run' THEN
        NEW.run_id := NEW.aggregate_id; NEW.task_id := NULL;
      ELSIF NEW.aggregate_type = 'task' THEN
        SELECT * INTO t FROM core_tasks WHERE id = NEW.aggregate_id;
        IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE = 'P4004', MESSAGE = 'task not found'; END IF;
        NEW.run_id := t.run_id; NEW.task_id := t.id;
      ELSE RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'unknown aggregate'; END IF;
      -- Every event for a Run is serialized, including events from separate Tasks.
      SELECT * INTO r FROM core_runs WHERE id = NEW.run_id FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE = 'P4004', MESSAGE = 'run not found'; END IF;
      IF NEW.aggregate_type = 'task' THEN
        SELECT * INTO t FROM core_tasks WHERE id = NEW.aggregate_id;
        actual_state := t.status; NEW.plan_id := t.plan_id; NEW.plan_version := t.plan_version;
      ELSE
        actual_state := r.status; NEW.plan_id := r.current_plan_id;
        SELECT version INTO NEW.plan_version FROM core_plans WHERE id = r.current_plan_id;
      END IF;
      IF NEW.aggregate_version = 0 THEN
        IF pg_trigger_depth() < 2 OR NEW.from_state IS NOT NULL OR
           NEW.to_state <> (CASE WHEN NEW.aggregate_type = 'run' THEN 'created' ELSE 'pending' END) THEN
          RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'creation event belongs to record creation'; END IF;
        NEW.event_type := NEW.aggregate_type || '.created';
      ELSE
        expected := NEW.aggregate_version - 1;
        IF expected <> (CASE WHEN NEW.aggregate_type = 'run' THEN r.version ELSE t.version END) THEN
          RAISE EXCEPTION USING ERRCODE = 'P4001', MESSAGE = 'version_conflict'; END IF;
        IF NEW.from_state IS DISTINCT FROM actual_state THEN
          RAISE EXCEPTION USING ERRCODE = 'P4001', MESSAGE = 'source state changed'; END IF;
        IF NEW.aggregate_type = 'run' THEN
          valid := CASE actual_state
            WHEN 'created' THEN NEW.to_state IN ('planning','failed','cancelled')
            WHEN 'planning' THEN NEW.to_state IN ('awaiting_confirm','running','paused','failed','cancelled')
            WHEN 'awaiting_confirm' THEN NEW.to_state IN ('planning','running','paused','failed','cancelled')
            WHEN 'running' THEN NEW.to_state IN ('planning','paused','completed','failed','cancelled')
            WHEN 'paused' THEN NEW.to_state IN ('failed','cancelled') OR NEW.to_state = r.resume_state
            ELSE false END;
          IF NEW.to_state = 'paused' AND (NEW.reason IS NULL OR NEW.reason NOT IN
              ('waiting_user','over_budget','user_paused','waiting_info')) THEN valid := false; END IF;
          IF NOT COALESCE(valid, false) THEN RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'invalid Run transition'; END IF;
          UPDATE core_runs SET status = NEW.to_state, version = NEW.aggregate_version,
            pause_reason = CASE WHEN NEW.to_state = 'paused' THEN NEW.reason END,
            resume_state = CASE WHEN NEW.to_state = 'paused' THEN actual_state END,
            cancel_reason = CASE WHEN NEW.to_state = 'cancelled' THEN NEW.reason ELSE cancel_reason END,
            execution_epoch = execution_epoch + 1,
            started_at = CASE WHEN NEW.to_state = 'running' THEN COALESCE(started_at, now()) ELSE started_at END,
            finished_at = CASE WHEN NEW.to_state IN ('completed','failed','cancelled') THEN now() END,
            updated_at = now() WHERE id = r.id AND version = expected;
        ELSE
          valid := CASE actual_state
            WHEN 'pending' THEN NEW.to_state IN ('validated','rejected','cancelled')
            WHEN 'validated' THEN NEW.to_state = 'cancelled' OR
              (t.kind = 'wait_user' AND NEW.to_state = 'waiting_user') OR
              (t.kind <> 'wait_user' AND NEW.to_state = 'queued')
            WHEN 'queued' THEN NEW.to_state IN ('submitted','failed','cancelled')
            WHEN 'submitted' THEN NEW.to_state IN ('succeeded','failed','blocked_by_model','cancelled')
            WHEN 'waiting_user' THEN NEW.to_state IN ('succeeded','cancelled')
            ELSE false END;
          IF NOT COALESCE(valid, false) THEN RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'invalid Task transition'; END IF;
          UPDATE core_tasks SET status = NEW.to_state, version = NEW.aggregate_version,
            execution_epoch = execution_epoch + 1,
            started_at = CASE WHEN NEW.to_state IN ('submitted','waiting_user') THEN COALESCE(started_at, now()) ELSE started_at END,
            finished_at = CASE WHEN NEW.to_state IN ('succeeded','failed','blocked_by_model','cancelled','rejected') THEN now() END,
            updated_at = now() WHERE id = t.id AND version = expected;
        END IF;
        GET DIAGNOSTICS changed = ROW_COUNT;
        IF changed <> 1 THEN RAISE EXCEPTION USING ERRCODE = 'P4001', MESSAGE = 'version_conflict'; END IF;
        NEW.event_type := NEW.aggregate_type || '.state_changed';
      END IF;
      UPDATE core_runs SET event_cursor = event_cursor + 1 WHERE id = r.id RETURNING event_cursor INTO seq;
      NEW.run_sequence := seq;
      NEW.correlation_id := COALESCE(NEW.correlation_id, r.id);
      NEW.payload := jsonb_strip_nulls(jsonb_build_object('run_id', NEW.run_id, 'task_id', NEW.task_id, 'plan_id', NEW.plan_id));
      RETURN NEW;
    END $$"""


def upgrade():
    op.add_column("core_event_outbox", sa.Column("event_kind", sa.String(8), nullable=False, server_default="state"))
    op.alter_column("core_event_outbox", "aggregate_version", nullable=True)
    op.alter_column("core_event_outbox", "to_state", nullable=True)
    op.create_check_constraint("ck_core_event_kind", "core_event_outbox",
        "(event_kind='state' AND aggregate_version IS NOT NULL AND to_state IS NOT NULL) OR "
        "(event_kind='domain' AND aggregate_version IS NULL AND from_state IS NULL AND to_state IS NULL AND causation_id IS NOT NULL)")
    op.execute(APPLY_EVENT)
    for statement in TABLE_DDL:
        op.execute(statement)


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM core_event_outbox WHERE event_kind='domain')
        OR EXISTS (SELECT 1 FROM core_event_timers WHERE status='scheduled')
        OR EXISTS (SELECT 1 FROM core_event_receipts WHERE status <> 'processed') THEN
        RAISE EXCEPTION 'Reconcile core events and timers before downgrade'; END IF; END $$""")
    for table in ("core_event_alerts", "core_event_timers", "core_event_replays", "core_event_dead_letters",
                  "core_event_attempts", "core_run_consumption", "core_event_receipts"):
        op.drop_table(table)
    op.execute(PREVIOUS_APPLY_EVENT)
    op.drop_constraint("ck_core_event_kind", "core_event_outbox")
    op.alter_column("core_event_outbox", "to_state", nullable=False)
    op.alter_column("core_event_outbox", "aggregate_version", nullable=False)
    op.drop_column("core_event_outbox", "event_kind")
