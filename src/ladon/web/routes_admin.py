"""The admin routes (D14, D15).

Admins come from the deployment config, not from the allow list, so a new
deployment has someone who can put the first person on it. Every decision here
writes an audit row through the same path the tool functions use.

Approving a request records the decision. Creating the namespace is an apply run
against OpenBao, which the apply endpoints own; this route does not reach the
vault, and the request sits in ``approved`` until that run picks it up.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ladon.datastore import repositories as repo
from ladon.datastore.models import NamespaceRequest
from ladon.mcp.server import _audit
from ladon.web.deps import AdminEmail, DbSession
from ladon.web.errors import ApiError
from ladon.web.identity import normalize_email
from ladon.web.serializers import allow_list_entry, namespace_request_row

router = APIRouter(prefix="/admin")


class RejectBody(BaseModel):
    reason: str


class AllowListBody(BaseModel):
    email: str
    notes: str | None = None


@router.get("/namespace-requests")
def list_namespace_requests(
    email: AdminEmail,
    session: DbSession,
    status: str | None = None,
) -> dict[str, Any]:
    rows = repo.list_namespace_requests(session, status=status)
    return {"requests": [namespace_request_row(r) for r in rows]}


@router.post("/namespace-requests/{request_id}/approve")
def approve_namespace_request(
    request_id: int,
    email: AdminEmail,
    session: DbSession,
) -> dict[str, Any]:
    row = _decide(
        session, request_id, lambda: repo.mark_request_approved(session, request_id=request_id)
    )
    _audit(
        session,
        user_email=email,
        tool_name="approve_namespace_request",
        status="ok",
        args={"request_id": request_id, "team_name": row.team_name},
    )
    session.commit()
    return namespace_request_row(row)


@router.post("/namespace-requests/{request_id}/reject")
def reject_namespace_request(
    request_id: int,
    body: RejectBody,
    email: AdminEmail,
    session: DbSession,
) -> dict[str, Any]:
    row = _decide(
        session,
        request_id,
        lambda: repo.mark_request_rejected(session, request_id=request_id, reason=body.reason),
    )
    _audit(
        session,
        user_email=email,
        tool_name="reject_namespace_request",
        status="ok",
        args={"request_id": request_id, "reason": body.reason},
    )
    session.commit()
    return namespace_request_row(row)


@router.get("/allow-list")
def list_allow_list(
    email: AdminEmail,
    session: DbSession,
) -> dict[str, Any]:
    return {"entries": [allow_list_entry(e) for e in repo.list_allowed(session)]}


@router.post("/allow-list")
def add_to_allow_list(
    body: AllowListBody,
    email: AdminEmail,
    session: DbSession,
) -> dict[str, Any]:
    added = normalize_email(body.email)
    if not added:
        raise ApiError(400, "an allow-list entry needs an email")
    entry = repo.allow(session, email=added, added_by=email, notes=body.notes)
    _audit(
        session,
        user_email=email,
        tool_name="allow_list_add",
        status="ok",
        args={"email": added},
    )
    session.commit()
    return allow_list_entry(entry)


@router.delete("/allow-list/{entry_email}", status_code=204)
def remove_from_allow_list(
    entry_email: str,
    email: AdminEmail,
    session: DbSession,
) -> Response:
    removed = normalize_email(entry_email)
    if not repo.deny(session, email=removed):
        raise ApiError(404, f"{removed} is not on the allow list")
    _audit(
        session,
        user_email=email,
        tool_name="allow_list_remove",
        status="ok",
        args={"email": removed},
    )
    session.commit()
    return Response(status_code=204)


def _decide(
    session: Session, request_id: int, decide: Callable[[], NamespaceRequest]
) -> NamespaceRequest:
    """Run a decision, turning an unknown request into a 404 rather than a 500."""
    try:
        return decide()
    except KeyError as exc:
        raise ApiError(404, f"namespace request {request_id} not found") from exc
