"""Datastore rows as the JSON the UI expects (D14).

One place where a row becomes a response body, so both the caller's view and the
admin's view of a namespace request are the same object. Timestamps go out as
ISO 8601 strings, which is what the client parses.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ladon.datastore.models import McpAllowListEntry, NamespaceRequest


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
