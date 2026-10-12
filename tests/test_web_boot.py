"""Tests for what happens before the backend serves a request (D14).

Migrations run at boot, against whatever ``SERO_DATABASE_URL`` names. SQLite is
the development target and the one a test can use, so the migration path is
exercised against a real file rather than mocked.

The route tests build their schema with ``Base.metadata.create_all``, which is
not the schema a deployment runs. So the tests here insert through every
repository, and serve two requests, against a database brought up by the
migrations: that is the only way a difference between the two shows up here
rather than on the first live write.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from fake_secrets import StubSecrets
from sero.datastore import repositories as repo
from sero.web.app import create_app
from sero.web.boot import MigrationError, run_migrations
from sero.web.settings import (
    DEFAULT_IDENTITY_HEADER,
    IdentitySettings,
    WebSettings,
    parse_access,
)

ADMIN = "admin@example.com"
MEMBER = "op@example.com"


def test_migrations_bring_an_empty_sqlite_file_to_head(tmp_path):
    url = f"sqlite:///{tmp_path / 'sero.db'}"
    assert run_migrations(url) is True

    tables = set(inspect(create_engine(url)).get_table_names())
    assert {"tool_call_log", "mcp_allow_list", "namespace_requests", "jobs"} <= tables
    # The stamp is what makes a second boot a no-op.
    assert "alembic_version" in tables


def test_running_migrations_twice_is_a_no_op(tmp_path):
    url = f"sqlite:///{tmp_path / 'sero.db'}"
    run_migrations(url)
    assert run_migrations(url) is True


def test_no_database_url_means_no_migrations(monkeypatch):
    monkeypatch.delenv("SERO_DATABASE_URL", raising=False)
    assert run_migrations() is False


def test_an_unusable_database_fails_loudly(tmp_path):
    # A directory that does not exist: the upgrade must be fatal, not skipped,
    # so the process never serves against a schema it could not reach.
    with pytest.raises(MigrationError):
        run_migrations(f"sqlite:///{tmp_path / 'absent' / 'sero.db'}")


def migrated(tmp_path):
    """A session factory over a database the migrations brought to head."""
    url = f"sqlite:///{tmp_path / 'sero.db'}"
    assert run_migrations(url) is True
    return sessionmaker(create_engine(url), expire_on_commit=False)


def test_a_migrated_database_assigns_ids_on_every_table(tmp_path):
    # A generated id that the database does not assign is the failure this
    # covers: it surfaces as a NOT NULL violation on the very first write of
    # any kind, and the id columns are the ones a migration can get wrong.
    sessions = migrated(tmp_path)
    later = datetime.now(UTC) + timedelta(hours=1)
    with sessions() as session:
        call = repo.append_call(
            session,
            user_email=ADMIN,
            method="tools/call",
            tool_name="allow_list_add",
            status="ok",
            duration_ms=0,
        )
        request = repo.create_namespace_request(
            session,
            requester_email=MEMBER,
            team_name="team-a",
            environments=["dev"],
            members=[MEMBER],
        )
        job = repo.create_job(session, kind="root-config", requested_by=ADMIN)
        entry = repo.allow(session, email=MEMBER, added_by=ADMIN)
        family = repo.create_family(
            session,
            user_email=MEMBER,
            client_id="sero-cli",
            audience="https://sero.example/mcp",
            scope="mcp",
            expires_at=later,
        )
        flow = repo.start_reveal_flow(
            session,
            state="flow-state",
            pkce_verifier="flow-verifier",
            requester_email=MEMBER,
        )
        grant = repo.grant_reveal(
            session,
            grant_id="grant-1",
            user_email=MEMBER,
            expires_at=later,
            auth_time=datetime.now(UTC),
            idp_assertion_id="assertion-1",
        )
        session.commit()

    assert call.id is not None
    assert request.id is not None
    assert job.id is not None
    # The rest key on a value the caller supplies, so what is proven here is
    # that the insert lands at all.
    assert (entry.email, flow.state, grant.id) == (MEMBER, "flow-state", "grant-1")
    assert family.jti


class OneNamespace(StubSecrets):
    """The secret reader's surface, as far as /api/namespaces needs it."""

    def list_namespaces(self, user_email):
        return ["team-a"]


def test_the_routes_serve_against_a_migrated_database(tmp_path):
    sessions = migrated(tmp_path)
    with sessions() as session:
        repo.allow(session, email=ADMIN, added_by=ADMIN)
        session.commit()

    settings = WebSettings(
        identity=IdentitySettings(),
        access=parse_access({"admins": [ADMIN], "members": {ADMIN: ["team-a"]}}),
        environment="dev",
        openbao_address="https://vault.example.com",
        kv_mount="kv",
        ui_dist=None,
        reveal_enabled=False,
    )
    app = create_app(settings, session_factory=sessions, secrets=OneNamespace())
    client = TestClient(app)
    headers = {DEFAULT_IDENTITY_HEADER: ADMIN}

    added = client.post("/api/admin/allow-list", json={"email": MEMBER}, headers=headers)
    assert added.status_code == 200, added.json()
    listed = client.get("/api/namespaces", headers=headers)
    assert listed.status_code == 200, listed.json()
    assert listed.json() == {"namespaces": ["team-a"]}

    with sessions() as session:
        names = [row.tool_name for row in repo.recent_calls(session)]
    assert names.count("allow_list_add") == 1
    assert names.count("list_my_namespaces") == 1
