"""Read/write operations over the datastore tables (D11).

Repositories are module-level functions that take a SQLAlchemy ``Session`` and
operate on it. There is no hidden global state and no framework: a caller opens a
session (via ``session_factory()``) and passes it in, which keeps these trivially
testable against SQLite and lets the caller own the transaction boundary.

This first slice covers two domains:

* the audit trail (``append_call``, ``recent_calls``), which redacts arguments
  through ``ladon.hygiene.redact`` before writing, so a secret never reaches the
  audit log;
* the MCP allow list (``allow``, ``deny``, ``is_allowed``, ``list_allowed``),
  the access-control list for the agent surface.

The refresh-token, namespace-request, and reveal-grant repositories follow.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ladon.datastore.models import McpAllowListEntry, McpRefreshToken, ToolCallLog
from ladon.hygiene import redact

# ── Audit trail ────────────────────────────────────────────────────────


def append_call(
    session: Session,
    *,
    user_email: str,
    method: str,
    tool_name: str,
    status: str,
    duration_ms: int,
    response_bytes: int | None = None,
    args: dict[str, Any] | None = None,
    error: str | None = None,
) -> ToolCallLog:
    """Append one audit row, redacting ``args`` before it is stored.

    ``args`` is the raw call arguments; they are passed through
    ``ladon.hygiene.redact`` so secret material never lands in the audit table.
    The row is added and flushed (so its id/timestamp are populated) but not
    committed: the caller owns the transaction.
    """
    row = ToolCallLog(
        user_email=user_email,
        method=method,
        tool_name=tool_name,
        status=status,
        duration_ms=duration_ms,
        response_bytes=response_bytes,
        redacted_args=redact(args) if args else None,
        error=error,
    )
    session.add(row)
    session.flush()
    return row


def recent_calls(
    session: Session,
    *,
    user_email: str | None = None,
    limit: int = 100,
) -> Sequence[ToolCallLog]:
    """Return the most recent audit rows, newest first, optionally by user.

    Ordered by timestamp then id descending; the id tie-breaks rows written in
    the same clock tick, so ordering is stable and deterministic.
    """
    stmt = select(ToolCallLog).order_by(ToolCallLog.ts.desc(), ToolCallLog.id.desc()).limit(limit)
    if user_email is not None:
        stmt = stmt.where(ToolCallLog.user_email == user_email)
    return session.scalars(stmt).all()


# ── MCP allow list ─────────────────────────────────────────────────────


def allow(
    session: Session,
    *,
    email: str,
    added_by: str,
    notes: str | None = None,
) -> McpAllowListEntry:
    """Add or update an allow-list entry for ``email``. Idempotent on the email."""
    entry = session.get(McpAllowListEntry, email)
    if entry is None:
        entry = McpAllowListEntry(email=email, added_by=added_by, notes=notes)
        session.add(entry)
    else:
        entry.added_by = added_by
        entry.notes = notes
    session.flush()
    return entry


def deny(session: Session, *, email: str) -> bool:
    """Remove ``email`` from the allow list. Returns True if a row was removed."""
    result = session.execute(delete(McpAllowListEntry).where(McpAllowListEntry.email == email))
    return result.rowcount > 0


def is_allowed(session: Session, email: str) -> bool:
    """Whether ``email`` is on the allow list."""
    return session.get(McpAllowListEntry, email) is not None


def list_allowed(session: Session) -> Sequence[McpAllowListEntry]:
    """All allow-list entries, ordered by email."""
    return session.scalars(select(McpAllowListEntry).order_by(McpAllowListEntry.email)).all()


# ── Refresh-token families (rotation + reuse detection) ─────────────────


def _new_id() -> str:
    """A random, URL-safe token identifier (a jti or a family id)."""
    return secrets.token_urlsafe(32)


def _short(token_id: str) -> str:
    """A short, non-reversible prefix of a token id, for logs and errors."""
    return f"{token_id[:6]}..."


@dataclass(frozen=True)
class FamilyMember:
    """A minted refresh-token family member: the family and this member's jti."""

    family_id: str
    jti: str


@dataclass(frozen=True)
class RotationResult:
    """The outcome of rotating a family: the new member and the carried claims."""

    family_id: str
    new_jti: str
    user_email: str
    client_id: str
    audience: str
    scope: str
    expires_at: datetime


class RefreshUnknownError(Exception):
    """The presented jti is not on file or its family was revoked."""


class RefreshReuseError(Exception):
    """The presented jti was already rotated: a replay. The family is revoked
    before this is raised."""

    def __init__(self, family_id: str) -> None:
        super().__init__(f"refresh-token replay on family {family_id}")
        self.family_id = family_id


def create_family(
    session: Session,
    *,
    user_email: str,
    client_id: str,
    audience: str,
    scope: str,
    expires_at: datetime,
) -> FamilyMember:
    """Mint a new refresh-token family for an authorization-code exchange.

    Returns the family id and the first member's jti, which the caller bakes into
    the refresh token it issues. Flushed, not committed: the caller owns the
    transaction.
    """
    family_id = _new_id()
    jti = _new_id()
    session.add(
        McpRefreshToken(
            jti=jti,
            family_id=family_id,
            user_email=user_email,
            client_id=client_id,
            audience=audience,
            scope=scope,
            expires_at=expires_at,
        )
    )
    session.flush()
    return FamilyMember(family_id=family_id, jti=jti)


def consume(session: Session, *, presented_jti: str, expires_at: datetime) -> RotationResult:
    """Rotate the presented refresh token, detecting replay.

    The presented row is locked for the transaction (``FOR UPDATE``) so two
    concurrent refreshes of the same jti cannot both succeed. Then:

    * unknown jti, or a family already revoked -> ``RefreshUnknownError``;
    * a jti that was already rotated -> a replay: the whole family is revoked
      atomically and ``RefreshReuseError`` is raised;
    * otherwise the presented jti is marked rotated and a new family member is
      inserted, and its details are returned.

    Flushed, not committed: the caller commits so the lock, the revoke/rotate,
    and the insert land as one transaction.
    """
    row = session.get(McpRefreshToken, presented_jti, with_for_update=True)
    if row is None:
        raise RefreshUnknownError(f"jti not found: {_short(presented_jti)}")
    if row.revoked_at is not None:
        raise RefreshUnknownError("family revoked")
    if row.rotated_at is not None:
        # Replay: a rotated token was presented again. Burn the whole family.
        revoke_family(session, family_id=row.family_id, reason="replay-detected")
        session.flush()
        raise RefreshReuseError(row.family_id)

    row.rotated_at = datetime.now(UTC)
    new_jti = _new_id()
    session.add(
        McpRefreshToken(
            jti=new_jti,
            family_id=row.family_id,
            user_email=row.user_email,
            client_id=row.client_id,
            audience=row.audience,
            scope=row.scope,
            expires_at=expires_at,
        )
    )
    session.flush()
    return RotationResult(
        family_id=row.family_id,
        new_jti=new_jti,
        user_email=row.user_email,
        client_id=row.client_id,
        audience=row.audience,
        scope=row.scope,
        expires_at=expires_at,
    )


def revoke_family(session: Session, *, family_id: str, reason: str = "manual") -> int:
    """Revoke every active member of a family. Returns the number revoked."""
    result = session.execute(
        update(McpRefreshToken)
        .where(McpRefreshToken.family_id == family_id, McpRefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC), revoked_reason=reason)
    )
    return result.rowcount or 0


def revoke_user(session: Session, *, user_email: str, reason: str = "manual") -> int:
    """Revoke every active family for a user. Returns the number revoked."""
    result = session.execute(
        update(McpRefreshToken)
        .where(McpRefreshToken.user_email == user_email, McpRefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC), revoked_reason=reason)
    )
    return result.rowcount or 0
