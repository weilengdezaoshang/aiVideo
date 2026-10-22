"""Full upgrade/rollback only against an explicitly supplied EMPTY disposable database."""
import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

URL = os.environ.get("AIVERO_CORE_MIGRATION_TEST_DB")
pytestmark = pytest.mark.skipif(not URL, reason="explicit EMPTY disposable migration database required")


def test_empty_database_upgrade_downgrade_and_reupgrade():
    engine = create_engine(URL)
    try:
        # Refuse populated databases rather than deleting anything to make the test pass.
        tables = set(inspect(engine).get_table_names())
        assert tables <= {"alembic_version"}, "migration fixture must start as an EMPTY database"
        if "alembic_version" in tables:
            with engine.connect() as db:
                assert db.scalar(text("SELECT count(*) FROM alembic_version")) == 0
        env = {**os.environ, "AIVERO_DB_URL": URL, "PYTHONPATH": "apps/api"}
        command = [str(Path(".venv/bin/alembic")), "-c", "alembic.ini"]
        def migrate(*args):
            subprocess.run([*command, *args], env=env, check=True, capture_output=True, text=True)
        migrate("upgrade", "head")
        assert "core_runs" in inspect(engine).get_table_names()
        assert {"core_run_id", "core_task_id"} <= {c["name"] for c in inspect(engine).get_columns("host_credit_ledger")}
        migrate("downgrade", "0014")
        assert not any(name.startswith("core_") for name in inspect(engine).get_table_names())
        assert "core_run_id" not in {c["name"] for c in inspect(engine).get_columns("host_credit_ledger")}
        with engine.connect() as db:
            assert db.scalar(text("SELECT count(*) FROM pg_proc WHERE proname LIKE 'core_%'")) == 0
        migrate("upgrade", "head")
        migrate("downgrade", "base")
        assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    finally:
        engine.dispose()
