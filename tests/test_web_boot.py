"""Tests for what happens before the backend serves a request (D14).

Migrations run at boot, against whatever ``LADON_DATABASE_URL`` names. SQLite is
the development target and the one a test can use, so the migration path is
exercised against a real file rather than mocked.
"""

import pytest
from sqlalchemy import create_engine, inspect

from ladon.web.boot import MigrationError, run_migrations


def test_migrations_bring_an_empty_sqlite_file_to_head(tmp_path):
    url = f"sqlite:///{tmp_path / 'ladon.db'}"
    assert run_migrations(url) is True

    tables = set(inspect(create_engine(url)).get_table_names())
    assert {"tool_call_log", "mcp_allow_list", "namespace_requests", "jobs"} <= tables
    # The stamp is what makes a second boot a no-op.
    assert "alembic_version" in tables


def test_running_migrations_twice_is_a_no_op(tmp_path):
    url = f"sqlite:///{tmp_path / 'ladon.db'}"
    run_migrations(url)
    assert run_migrations(url) is True


def test_no_database_url_means_no_migrations(monkeypatch):
    monkeypatch.delenv("LADON_DATABASE_URL", raising=False)
    assert run_migrations() is False


def test_an_unusable_database_fails_loudly(tmp_path):
    # A directory that does not exist: the upgrade must be fatal, not skipped,
    # so the process never serves against a schema it could not reach.
    with pytest.raises(MigrationError):
        run_migrations(f"sqlite:///{tmp_path / 'absent' / 'ladon.db'}")
