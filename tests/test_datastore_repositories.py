"""Tests for the datastore repositories (D11): audit trail and allow list.

Run against in-memory SQLite; no PostgreSQL needed.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from sero.datastore import repositories as repo
from sero.datastore.models import Base
from sero.hygiene import REDACTED


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


# ── Audit ──────────────────────────────────────────────────────────────


def test_append_call_redacts_args_before_storing(session):
    row = repo.append_call(
        session,
        user_email="op@example.com",
        method="tools/call",
        tool_name="create_secret",
        status="ok",
        duration_ms=12,
        args={"path": "team-a/app", "data": {"API_KEY": "supersecretvalue"}},
    )
    session.commit()
    assert row.id is not None
    assert row.ts is not None
    # The secret-bearing "data" key is redacted; the path name is kept.
    assert row.redacted_args["path"] == "team-a/app"
    assert row.redacted_args["data"] == REDACTED
    assert "supersecretvalue" not in str(row.redacted_args)


def test_append_call_without_args_stores_null(session):
    row = repo.append_call(
        session,
        user_email="op@example.com",
        method="tools/list",
        tool_name="list_secrets",
        status="ok",
        duration_ms=3,
    )
    assert row.redacted_args is None


def test_recent_calls_newest_first_and_filtered(session):
    for i in range(3):
        repo.append_call(
            session,
            user_email="a@example.com",
            method="m",
            tool_name=f"tool{i}",
            status="ok",
            duration_ms=1,
        )
    repo.append_call(
        session,
        user_email="b@example.com",
        method="m",
        tool_name="other",
        status="ok",
        duration_ms=1,
    )
    session.commit()

    all_rows = repo.recent_calls(session)
    assert len(all_rows) == 4
    # Newest first: the last inserted (b's "other") leads.
    assert all_rows[0].tool_name == "other"

    only_a = repo.recent_calls(session, user_email="a@example.com")
    assert len(only_a) == 3
    assert all(r.user_email == "a@example.com" for r in only_a)

    limited = repo.recent_calls(session, limit=2)
    assert len(limited) == 2


# ── Allow list ─────────────────────────────────────────────────────────


def test_allow_then_is_allowed(session):
    assert repo.is_allowed(session, "op@example.com") is False
    repo.allow(session, email="op@example.com", added_by="admin@example.com", notes="team lead")
    session.commit()
    assert repo.is_allowed(session, "op@example.com") is True


def test_allow_is_idempotent_and_updates(session):
    repo.allow(session, email="op@example.com", added_by="admin@example.com")
    repo.allow(session, email="op@example.com", added_by="admin2@example.com", notes="updated")
    session.commit()
    entries = repo.list_allowed(session)
    assert len(entries) == 1  # no duplicate row
    assert entries[0].added_by == "admin2@example.com"
    assert entries[0].notes == "updated"


def test_deny_removes(session):
    repo.allow(session, email="op@example.com", added_by="admin@example.com")
    session.commit()
    assert repo.deny(session, email="op@example.com") is True
    session.commit()
    assert repo.is_allowed(session, "op@example.com") is False
    # Denying an absent entry reports no removal.
    assert repo.deny(session, email="nobody@example.com") is False


def test_list_allowed_ordered_by_email(session):
    for email in ("c@example.com", "a@example.com", "b@example.com"):
        repo.allow(session, email=email, added_by="admin@example.com")
    session.commit()
    assert [e.email for e in repo.list_allowed(session)] == [
        "a@example.com",
        "b@example.com",
        "c@example.com",
    ]
