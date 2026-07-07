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

from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ladon.datastore.models import McpAllowListEntry, ToolCallLog
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
