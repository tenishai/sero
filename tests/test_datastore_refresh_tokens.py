"""Tests for the refresh-token repository (D11): rotation and reuse detection.

The security-critical bit: presenting an already-rotated token is a replay and
burns the whole family. Run against in-memory SQLite.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from sero.datastore import repositories as repo
from sero.datastore.models import Base, McpRefreshToken


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _expiry(hours: int = 1) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)


def make_family(session):
    return repo.create_family(
        session,
        user_email="op@example.com",
        client_id="sero-cli",
        audience="sero",
        scope="read",
        expires_at=_expiry(),
    )


def test_create_family_mints_distinct_ids(session):
    fam = make_family(session)
    assert fam.family_id and fam.jti
    assert fam.family_id != fam.jti
    stored = session.get(McpRefreshToken, fam.jti)
    assert stored.family_id == fam.family_id
    assert stored.rotated_at is None and stored.revoked_at is None


def test_consume_rotates_and_carries_claims(session):
    fam = make_family(session)
    result = repo.consume(session, presented_jti=fam.jti, expires_at=_expiry(2))

    assert result.family_id == fam.family_id
    assert result.new_jti != fam.jti
    assert result.user_email == "op@example.com"
    assert result.scope == "read"
    # Old member is now marked rotated; new member exists, not rotated.
    old = session.get(McpRefreshToken, fam.jti)
    assert old.rotated_at is not None
    new = session.get(McpRefreshToken, result.new_jti)
    assert new is not None and new.rotated_at is None


def test_chain_of_rotations(session):
    fam = make_family(session)
    r1 = repo.consume(session, presented_jti=fam.jti, expires_at=_expiry())
    r2 = repo.consume(session, presented_jti=r1.new_jti, expires_at=_expiry())
    assert r2.family_id == fam.family_id
    assert len({fam.jti, r1.new_jti, r2.new_jti}) == 3


def test_replay_burns_the_whole_family(session):
    fam = make_family(session)
    r1 = repo.consume(session, presented_jti=fam.jti, expires_at=_expiry())

    # Presenting the already-rotated original jti again is a replay.
    with pytest.raises(repo.RefreshReuseError) as exc:
        repo.consume(session, presented_jti=fam.jti, expires_at=_expiry())
    assert exc.value.family_id == fam.family_id

    # Every member of the family is now revoked, including the still-current one.
    members = session.scalars(
        select(McpRefreshToken).where(McpRefreshToken.family_id == fam.family_id)
    ).all()
    assert members, "family should still have rows"
    assert all(m.revoked_at is not None for m in members)
    assert all(m.revoked_reason == "replay-detected" for m in members)

    # The previously-valid rotated token can no longer be used.
    with pytest.raises(repo.RefreshUnknownError):
        repo.consume(session, presented_jti=r1.new_jti, expires_at=_expiry())


def test_unknown_jti_rejected(session):
    with pytest.raises(repo.RefreshUnknownError, match="not found"):
        repo.consume(session, presented_jti="nope", expires_at=_expiry())


def test_revoked_family_rejects_consume(session):
    fam = make_family(session)
    assert repo.revoke_family(session, family_id=fam.family_id) == 1
    with pytest.raises(repo.RefreshUnknownError, match="revoked"):
        repo.consume(session, presented_jti=fam.jti, expires_at=_expiry())


def test_revoke_family_counts_active_only(session):
    fam = make_family(session)
    repo.consume(session, presented_jti=fam.jti, expires_at=_expiry())  # 2 rows now
    revoked = repo.revoke_family(session, family_id=fam.family_id, reason="manual")
    assert revoked == 2
    # A second revoke affects nothing (all already revoked).
    assert repo.revoke_family(session, family_id=fam.family_id) == 0


def test_revoke_user_across_families(session):
    fam1 = make_family(session)
    fam2 = make_family(session)
    assert fam1.family_id != fam2.family_id
    revoked = repo.revoke_user(session, user_email="op@example.com")
    assert revoked == 2
    for fam in (fam1, fam2):
        assert session.get(McpRefreshToken, fam.jti).revoked_at is not None


def test_error_messages_never_leak_full_jti(session):
    # The presented jti is only ever shown as a short, non-reversible prefix.
    long_jti = "abcdef0123456789-this-should-not-appear-in-full"
    with pytest.raises(repo.RefreshUnknownError) as exc:
        repo.consume(session, presented_jti=long_jti, expires_at=_expiry())
    assert long_jti not in str(exc.value)
    assert "abcdef" in str(exc.value)  # short prefix is fine
