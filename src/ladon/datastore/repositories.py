"""Read/write operations over the datastore tables (D11).

Repositories are module-level functions that take a SQLAlchemy ``Session`` and
operate on it. There is no hidden global state and no framework: a caller opens a
session (via ``session_factory()``) and passes it in, which keeps these trivially
testable against SQLite and lets the caller own the transaction boundary.

Covers the five datastore domains:

* the audit trail (``append_call``, ``recent_calls``), which redacts arguments
  through ``ladon.hygiene.redact`` before writing, so a secret never reaches the
  audit log;
* the MCP allow list (``allow``, ``deny``, ``is_allowed``, ``list_allowed``),
  the access-control list for the agent surface;
* refresh-token families (``create_family``, ``consume``, ``revoke_family``,
  ``revoke_user``), with rotation and replay detection;
* the namespace-request workflow (``create_namespace_request``,
  ``mark_request_approved``, ``mark_request_rejected``, ``mark_request_submitted``,
  ``mark_request_failed``, ``record_request_job``, ``record_request_error``,
  ``list_namespace_requests``);
* step-up reveal flows and grants (``start_reveal_flow``, ``consume_reveal_flow``,
  ``grant_reveal``, ``active_reveal_grant``, ``revoke_reveal_grant``);
* background apply jobs (``create_job``, ``mark_job_running``, ``mark_job_done``,
  ``mark_job_failed``, ``get_job``, ``active_job``, ``list_jobs``,
  ``fail_unfinished_jobs``).
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ladon.datastore.models import (
    Job,
    McpAllowListEntry,
    McpRefreshToken,
    NamespaceRequest,
    RevealFlow,
    RevealGrant,
    ToolCallLog,
)
from ladon.hygiene import redact, redact_text

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
    ``error`` goes through ``redact_text`` for the same reason: an error message
    is written by many hands and one that quotes what it was given would carry a
    token into this table. The row is added and flushed (so its id/timestamp are
    populated) but not committed: the caller owns the transaction.
    """
    row = ToolCallLog(
        user_email=user_email,
        method=method,
        tool_name=tool_name,
        status=status,
        duration_ms=duration_ms,
        response_bytes=response_bytes,
        redacted_args=redact(args) if args else None,
        error=redact_text(error) if error else None,
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


# ── Namespace-request workflow ──────────────────────────────────────────


def create_namespace_request(
    session: Session,
    *,
    requester_email: str,
    team_name: str,
    environments: list[str],
    members: list[str],
    description: str | None = None,
) -> NamespaceRequest:
    """Open a namespace request in the ``pending`` state.

    Flushed, not committed: the caller owns the transaction and gets the row back
    with its assigned id.
    """
    row = NamespaceRequest(
        requester_email=requester_email,
        team_name=team_name,
        environments=environments,
        members=members,
        description=description,
        status="pending",
    )
    session.add(row)
    session.flush()
    return row


def mark_request_submitted(
    session: Session, *, request_id: int, ticket_ref: str
) -> NamespaceRequest:
    """Move a request to ``submitted`` and record the tracking ticket reference."""
    row = _require_request(session, request_id)
    row.status = "submitted"
    row.ticket_ref = ticket_ref
    row.updated_at = datetime.now(UTC)
    session.flush()
    return row


def mark_request_approved(session: Session, *, request_id: int) -> NamespaceRequest:
    """Move a request to ``approved``: an admin has said yes.

    Approval is the decision, not the provisioning. Creating the namespace is a
    separate apply run that reads approved requests.
    """
    row = _require_request(session, request_id)
    row.status = "approved"
    row.updated_at = datetime.now(UTC)
    session.flush()
    return row


def mark_request_rejected(session: Session, *, request_id: int, reason: str) -> NamespaceRequest:
    """Move a request to ``rejected`` and record why.

    The reason lands in ``error``, the column that already holds why a request
    did not proceed, so a rejected request reads the same way as a failed one.
    """
    row = _require_request(session, request_id)
    row.status = "rejected"
    row.error = reason
    row.updated_at = datetime.now(UTC)
    session.flush()
    return row


def mark_request_failed(session: Session, *, request_id: int, error: str) -> NamespaceRequest:
    """Move a request to ``failed`` and record the error."""
    row = _require_request(session, request_id)
    row.status = "failed"
    row.error = error
    row.updated_at = datetime.now(UTC)
    session.flush()
    return row


def record_request_job(session: Session, *, request_id: int, job_id: int) -> NamespaceRequest:
    """Point an approved request at the apply job that carries it out."""
    row = _require_request(session, request_id)
    row.job_id = job_id
    row.updated_at = datetime.now(UTC)
    session.flush()
    return row


def record_request_error(
    session: Session, *, request_id: int, error: str | None
) -> NamespaceRequest:
    """Record why a request's apply run did not land, leaving its status alone.

    The decision and the run are separate things: an admin approved this, and
    that stands. What failed is the run, and the row says so where the admin is
    already looking. ``None`` clears it, which is what a run that lands does to
    the last one's failure. Errors name the object at fault, never a value.
    """
    row = _require_request(session, request_id)
    row.error = redact_text(error) if error else None
    row.updated_at = datetime.now(UTC)
    session.flush()
    return row


def get_namespace_request(session: Session, request_id: int) -> NamespaceRequest:
    """One request by id. Raises ``KeyError`` when there is no such request."""
    return _require_request(session, request_id)


def _require_request(session: Session, request_id: int) -> NamespaceRequest:
    row = session.get(NamespaceRequest, request_id)
    if row is None:
        raise KeyError(f"namespace request {request_id} not found")
    return row


def list_namespace_requests(
    session: Session, *, status: str | None = None, limit: int = 100
) -> Sequence[NamespaceRequest]:
    """List namespace requests, newest first, optionally filtered by status."""
    stmt = (
        select(NamespaceRequest)
        .order_by(NamespaceRequest.created_at.desc(), NamespaceRequest.id.desc())
        .limit(limit)
    )
    if status is not None:
        stmt = stmt.where(NamespaceRequest.status == status)
    return session.scalars(stmt).all()


# ── Background apply jobs ───────────────────────────────────────────────

# A job that has not reached an ending yet. One job per kind runs at a time, and
# these are the two states that count as "one is running".
ACTIVE_JOB_STATES = ("queued", "running")


def create_job(session: Session, *, kind: str, requested_by: str) -> Job:
    """Open a job in the ``queued`` state. Flushed, not committed."""
    row = Job(kind=kind, state="queued", requested_by=requested_by)
    session.add(row)
    session.flush()
    return row


def mark_job_running(session: Session, *, job_id: int) -> Job:
    """Move a job to ``running`` and stamp its start time."""
    row = _require_job(session, job_id)
    row.state = "running"
    row.started_at = datetime.now(UTC)
    session.flush()
    return row


def mark_job_done(session: Session, *, job_id: int, report: dict[str, Any] | None = None) -> Job:
    """Move a job to ``done``, recording the op-by-op report. Never a value."""
    row = _require_job(session, job_id)
    row.state = "done"
    row.report_json = report
    row.finished_at = datetime.now(UTC)
    session.flush()
    return row


def mark_job_failed(
    session: Session, *, job_id: int, error: str, report: dict[str, Any] | None = None
) -> Job:
    """Move a job to ``failed``, recording the error and whatever it did do.

    The error goes through ``redact_text`` for the same reason the audit
    trail's does: a message is written by many hands, and one that quotes what
    it was given would carry a token into this table. A failed run still
    carries its report: what it managed to do before it stopped is what an
    operator has to be told.
    """
    row = _require_job(session, job_id)
    row.state = "failed"
    row.error = redact_text(error)
    if report is not None:
        row.report_json = report
    row.finished_at = datetime.now(UTC)
    session.flush()
    return row


def get_job(session: Session, job_id: int) -> Job | None:
    """One job by id, or None when there is no such job."""
    return session.get(Job, job_id)


def active_job(session: Session, *, kind: str) -> Job | None:
    """The job of this kind that has not finished, if there is one.

    One apply of a kind runs at a time: two of them converge the same objects
    against each other's half-written state.
    """
    stmt = (
        select(Job)
        .where(Job.kind == kind, Job.state.in_(ACTIVE_JOB_STATES))
        .order_by(Job.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def fail_unfinished_jobs(session: Session, *, error: str) -> int:
    """End every job that has not finished, and say how many there were.

    A job is a thread inside one process, so a restart takes any that were in
    flight with it. The rows they left behind say ``running``, which is both
    untrue and, because one job of a kind runs at a time, enough to refuse
    every later job of that kind. Flushed, not committed: the caller owns the
    transaction.
    """
    result = session.execute(
        update(Job)
        .where(Job.state.in_(ACTIVE_JOB_STATES))
        .values(state="failed", error=redact_text(error), finished_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    session.flush()
    # A bulk update goes round any object the session is already holding, so
    # everything is expired and read again rather than left saying what it said
    # before. This runs once at startup, where there is nothing to lose.
    session.expire_all()
    return int(result.rowcount or 0)


def list_jobs(session: Session, *, kind: str | None = None, limit: int = 50) -> Sequence[Job]:
    """Recent jobs, newest first, optionally of one kind."""
    stmt = select(Job).order_by(Job.created_at.desc(), Job.id.desc()).limit(limit)
    if kind is not None:
        stmt = stmt.where(Job.kind == kind)
    return session.scalars(stmt).all()


def _require_job(session: Session, job_id: int) -> Job:
    row = session.get(Job, job_id)
    if row is None:
        raise KeyError(f"job {job_id} not found")
    return row


# ── Step-up reveal flows and grants ─────────────────────────────────────

# How long an in-progress reveal auth flow stays valid before it is stale.
DEFAULT_FLOW_TTL = timedelta(minutes=10)


def start_reveal_flow(
    session: Session, *, state: str, pkce_verifier: str, requester_email: str
) -> RevealFlow:
    """Record an in-progress step-up reveal flow (short-lived PKCE state)."""
    row = RevealFlow(state=state, pkce_verifier=pkce_verifier, requester_email=requester_email)
    session.add(row)
    session.flush()
    return row


def consume_reveal_flow(
    session: Session,
    *,
    state: str,
    now: datetime | None = None,
    ttl: timedelta = DEFAULT_FLOW_TTL,
) -> RevealFlow | None:
    """Pop a reveal flow by ``state``, if present and not older than ``ttl``.

    Single-use: the row is deleted whether or not it was within the TTL, so a
    given state can never be consumed twice. Returns the flow only when it was
    still fresh; a stale flow is deleted and None is returned.
    """
    now = now or datetime.now(UTC)
    row = session.get(RevealFlow, state)
    if row is None:
        return None
    fresh = row.created_at is not None and (now - _aware(row.created_at)) <= ttl
    session.delete(row)
    session.flush()
    return row if fresh else None


def grant_reveal(
    session: Session,
    *,
    grant_id: str,
    user_email: str,
    expires_at: datetime,
    auth_time: datetime,
    idp_assertion_id: str,
) -> RevealGrant:
    """Record a step-up reveal grant: that access was granted, not the secret."""
    row = RevealGrant(
        id=grant_id,
        user_email=user_email,
        expires_at=expires_at,
        auth_time=auth_time,
        idp_assertion_id=idp_assertion_id,
    )
    session.add(row)
    session.flush()
    return row


def active_reveal_grant(
    session: Session,
    *,
    grant_id: str,
    user_email: str,
    now: datetime | None = None,
) -> RevealGrant | None:
    """Return the grant only if it matches the user, is unrevoked, and unexpired.

    A grant belonging to another user, revoked, or past its expiry reads as no
    active grant (returns None) rather than an error, so callers treat all three
    the same: access is not currently granted.
    """
    now = now or datetime.now(UTC)
    row = session.get(RevealGrant, grant_id)
    if row is None or row.user_email != user_email:
        return None
    if row.revoked_at is not None:
        return None
    if _aware(row.expires_at) <= now:
        return None
    return row


def revoke_reveal_grant(session: Session, *, grant_id: str, user_email: str) -> bool:
    """Revoke a grant for a user. Returns True if an active grant was revoked."""
    result = session.execute(
        update(RevealGrant)
        .where(
            RevealGrant.id == grant_id,
            RevealGrant.user_email == user_email,
            RevealGrant.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )
    return (result.rowcount or 0) > 0


def _aware(dt: datetime) -> datetime:
    """Treat a naive datetime as UTC, so comparisons never mix aware and naive.

    SQLite round-trips timezone-aware datetimes as naive; PostgreSQL preserves
    the tz. Normalizing here keeps the expiry checks correct on both.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
