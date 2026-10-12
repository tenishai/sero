"""Tests for the datastore models (D11).

Pure schema tests: create the tables in an in-memory SQLite database (SQLAlchemy
is dialect-agnostic for this schema) and round-trip a row per table. No
PostgreSQL and no live infrastructure required.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from sero.datastore.models import (
    Base,
    McpAllowListEntry,
    McpRefreshToken,
    NamespaceRequest,
    RevealFlow,
    RevealGrant,
    ToolCallLog,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_every_table_is_registered():
    assert set(Base.metadata.tables) == {
        "tool_call_log",
        "mcp_refresh_tokens",
        "mcp_allow_list",
        "namespace_requests",
        "reveal_flows",
        "reveal_grants",
        "jobs",
    }


def test_audit_log_round_trip(session):
    row = ToolCallLog(
        user_email="op@example.com",
        method="tools/call",
        tool_name="create_secret",
        status="ok",
        duration_ms=42,
        redacted_args={"path": "team-a/app", "keys": ["API_KEY"]},
    )
    session.add(row)
    session.commit()
    got = session.scalars(select(ToolCallLog)).one()
    assert got.id is not None  # autoincrement
    assert got.ts is not None  # server_default now()
    assert got.redacted_args["keys"] == ["API_KEY"]
    # No secret value column exists; only redacted args.
    assert "value" not in {c.name for c in ToolCallLog.__table__.columns}


def test_refresh_token_recorded_by_identifier_only(session):
    now = datetime.now(UTC)
    session.add(
        McpRefreshToken(
            jti="jti-1",
            family_id="fam-1",
            user_email="op@example.com",
            client_id="sero-cli",
            audience="sero",
            expires_at=now + timedelta(hours=1),
        )
    )
    session.commit()
    got = session.get(McpRefreshToken, "jti-1")
    assert got.family_id == "fam-1"
    assert got.scope == ""  # server_default
    assert got.revoked_at is None
    # The token material itself is never a column.
    cols = {c.name for c in McpRefreshToken.__table__.columns}
    assert "token" not in cols and "refresh_token" not in cols


def test_allow_list_entry(session):
    session.add(McpAllowListEntry(email="op@example.com", added_by="admin@example.com"))
    session.commit()
    assert session.get(McpAllowListEntry, "op@example.com").added_at is not None


def test_namespace_request_defaults_pending(session):
    row = NamespaceRequest(
        requester_email="op@example.com",
        team_name="team-a",
        environments=["dev", "prod"],
        members=["op@example.com"],
    )
    session.add(row)
    session.commit()
    got = session.scalars(select(NamespaceRequest)).one()
    assert got.status == "pending"
    assert got.environments == ["dev", "prod"]
    assert got.ticket_ref is None


def test_reveal_flow_and_grant(session):
    now = datetime.now(UTC)
    session.add(RevealFlow(state="st-1", pkce_verifier="v", requester_email="op@example.com"))
    session.add(
        RevealGrant(
            id="g-1",
            user_email="op@example.com",
            expires_at=now + timedelta(minutes=5),
            auth_time=now,
            idp_assertion_id="assertion-1",
        )
    )
    session.commit()
    grant = session.get(RevealGrant, "g-1")
    assert grant.idp_assertion_id == "assertion-1"
    assert grant.revoked_at is None
    # A grant records that access was granted, not the secret revealed.
    cols = {c.name for c in RevealGrant.__table__.columns}
    assert "secret" not in cols and "value" not in cols


def test_migration_head_matches_models():
    # The committed migrations must between them create every model's table, so
    # a fresh DB migrated to head equals Base.metadata. Guards against a model
    # added without a migration.
    from pathlib import Path

    versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    src = "\n".join(p.read_text() for p in sorted(versions.glob("*.py")))
    for table in Base.metadata.tables:
        assert f"create_table('{table}'" in src, f"migration missing create_table for {table}"
