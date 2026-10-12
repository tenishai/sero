"""Datastore rows as the JSON the UI expects (D14).

One place where a row becomes a response body, so both the caller's view and the
admin's view of a namespace request are the same object. Timestamps go out as
ISO 8601 strings, which is what the client parses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sero.datastore.models import Job, McpAllowListEntry, NamespaceRequest
from sero.web.jobs import OPERATIONS


def namespace_request_row(row: NamespaceRequest) -> dict[str, Any]:
    return {
        "id": row.id,
        "requester_email": row.requester_email,
        "team_name": row.team_name,
        "environments": list(row.environments or []),
        "members": list(row.members or []),
        "description": row.description,
        "status": row.status,
        "created_at": _iso(row.created_at),
        # The apply run an approval started, and why it did not land. A
        # rejection's reason lands in the same column, so a row that did not
        # proceed reads the same way whichever way it stopped.
        "job_id": row.job_id,
        "error": row.error,
    }


def job_row(row: Job, *, now: datetime | None = None) -> dict[str, Any]:
    """One background job as the caller polls it.

    ``elapsed_seconds`` is computed here rather than stored: a client that has
    to subtract two timestamps has to agree with the server about the clock,
    and a job still running has no end to subtract from yet.
    """
    started = row.started_at or row.created_at
    ended = row.finished_at or now or datetime.now(UTC)
    return {
        "id": row.id,
        "kind": row.kind,
        "state": row.state,
        "requested_by": row.requested_by,
        "created_at": _iso(row.created_at),
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "elapsed_seconds": round(max((_utc(ended) - _utc(started)).total_seconds(), 0.0), 3),
        "report": list((row.report_json or {}).get(OPERATIONS, [])),
        "error": row.error,
    }


def allow_list_entry(row: McpAllowListEntry) -> dict[str, Any]:
    return {
        "email": row.email,
        "added_by": row.added_by,
        "added_at": _iso(row.added_at),
        "notes": row.notes,
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _utc(value: datetime) -> datetime:
    """A timestamp as an aware one. SQLite hands back what it stored, which is
    UTC without the offset; PostgreSQL keeps it."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
