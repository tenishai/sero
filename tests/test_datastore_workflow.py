"""Tests for the namespace-request and reveal-grant repositories (D11).

Run against in-memory SQLite. Expiry-dependent logic is tested with an explicit
``now`` so it is deterministic.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ladon.datastore import repositories as repo
from ladon.datastore.models import Base


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


# ── Namespace requests ─────────────────────────────────────────────────


def make_request(session, team="team-a"):
    return repo.create_namespace_request(
        session,
        requester_email="op@example.com",
        team_name=team,
        environments=["dev", "prod"],
        members=["op@example.com"],
        description="new team",
    )


def test_create_starts_pending(session):
    row = make_request(session)
    session.commit()
    assert row.id is not None
    assert row.status == "pending"
    assert row.environments == ["dev", "prod"]
    assert row.ticket_ref is None and row.error is None


def test_mark_submitted_records_ticket(session):
    row = make_request(session)
    session.commit()
    updated = repo.mark_request_submitted(session, request_id=row.id, ticket_ref="TKT-1")
    assert updated.status == "submitted"
    assert updated.ticket_ref == "TKT-1"


def test_mark_failed_records_error(session):
    row = make_request(session)
    session.commit()
    updated = repo.mark_request_failed(session, request_id=row.id, error="quota exceeded")
    assert updated.status == "failed"
    assert updated.error == "quota exceeded"


def test_mark_unknown_request_raises(session):
    with pytest.raises(KeyError, match="not found"):
        repo.mark_request_submitted(session, request_id=999, ticket_ref="x")


def test_list_requests_filter_and_order(session):
    a = make_request(session, team="a")
    make_request(session, team="b")
    session.commit()
    repo.mark_request_failed(session, request_id=a.id, error="e")
    session.commit()

    assert len(repo.list_namespace_requests(session)) == 2
    failed = repo.list_namespace_requests(session, status="failed")
    assert [r.team_name for r in failed] == ["a"]
    pending = repo.list_namespace_requests(session, status="pending")
    assert [r.team_name for r in pending] == ["b"]


# ── Reveal flows ───────────────────────────────────────────────────────


def test_consume_flow_returns_fresh_and_is_single_use(session):
    repo.start_reveal_flow(
        session, state="st-1", pkce_verifier="verifier", requester_email="op@example.com"
    )
    session.commit()
    got = repo.consume_reveal_flow(session, state="st-1")
    assert got is not None
    assert got.pkce_verifier == "verifier"
    # Single-use: a second consume finds nothing.
    assert repo.consume_reveal_flow(session, state="st-1") is None


def test_consume_stale_flow_returns_none_but_deletes(session):
    created = datetime.now(UTC) - timedelta(minutes=30)
    # Insert directly with an old created_at to simulate a stale flow.
    from ladon.datastore.models import RevealFlow

    session.add(
        RevealFlow(
            state="old", pkce_verifier="v", requester_email="op@example.com", created_at=created
        )
    )
    session.commit()
    assert repo.consume_reveal_flow(session, state="old", ttl=timedelta(minutes=10)) is None
    # It was consumed (deleted) even though stale, so it cannot be retried.
    assert session.get(RevealFlow, "old") is None


def test_consume_unknown_flow_returns_none(session):
    assert repo.consume_reveal_flow(session, state="nope") is None


# ── Reveal grants ──────────────────────────────────────────────────────


def make_grant(session, now, ttl_minutes=15):
    return repo.grant_reveal(
        session,
        grant_id="g-1",
        user_email="op@example.com",
        expires_at=now + timedelta(minutes=ttl_minutes),
        auth_time=now,
        idp_assertion_id="assert-1",
    )


def test_active_grant_within_window(session):
    now = datetime.now(UTC)
    make_grant(session, now)
    session.commit()
    got = repo.active_reveal_grant(session, grant_id="g-1", user_email="op@example.com", now=now)
    assert got is not None
    assert got.idp_assertion_id == "assert-1"


def test_expired_grant_is_not_active(session):
    now = datetime.now(UTC)
    make_grant(session, now, ttl_minutes=5)
    session.commit()
    later = now + timedelta(minutes=10)
    assert (
        repo.active_reveal_grant(session, grant_id="g-1", user_email="op@example.com", now=later)
        is None
    )


def test_wrong_user_is_not_active(session):
    now = datetime.now(UTC)
    make_grant(session, now)
    session.commit()
    assert (
        repo.active_reveal_grant(session, grant_id="g-1", user_email="someone@else.com", now=now)
        is None
    )


def test_revoked_grant_is_not_active(session):
    now = datetime.now(UTC)
    make_grant(session, now)
    session.commit()
    assert repo.revoke_reveal_grant(session, grant_id="g-1", user_email="op@example.com") is True
    session.commit()
    assert (
        repo.active_reveal_grant(session, grant_id="g-1", user_email="op@example.com", now=now)
        is None
    )
    # Revoking again finds nothing active.
    assert repo.revoke_reveal_grant(session, grant_id="g-1", user_email="op@example.com") is False


def test_approve_and_reject_record_the_decision(session):
    approved = repo.create_namespace_request(
        session,
        requester_email="op@example.com",
        team_name="team-a",
        environments=["dev"],
        members=["op@example.com"],
    )
    rejected = repo.create_namespace_request(
        session,
        requester_email="op@example.com",
        team_name="team-b",
        environments=["dev"],
        members=["op@example.com"],
    )

    assert repo.mark_request_approved(session, request_id=approved.id).status == "approved"
    row = repo.mark_request_rejected(session, request_id=rejected.id, reason="use team-a")
    assert row.status == "rejected"
    assert row.error == "use team-a"
