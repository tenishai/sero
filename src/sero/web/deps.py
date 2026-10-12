"""What every route needs, resolved once per request (D14).

The collaborators an instance was built with live on ``app.state`` as
:class:`Services`; these dependencies read them and turn a missing one into the
right answer rather than a traceback: no identity is 401, an unconfigured
datastore or vault is 503, and a caller who is not an admin is 403.

Identity is resolved before anything else in every route signature, so a request
with no identity is refused before the backend looks at a body, a namespace, or
a database.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from sero.web.audit import DENIED, UNIDENTIFIED, AuditRecorder
from sero.web.errors import ApiError
from sero.web.groups import GroupSource
from sero.web.identity import IdentityReader, NoIdentityError
from sero.web.jobs import JobRunner
from sero.web.reveal import RevealGate
from sero.web.settings import WebSettings


@dataclass(frozen=True)
class Services:
    """The collaborators one backend instance runs with."""

    settings: WebSettings
    identity: IdentityReader
    groups: GroupSource
    reveal_gate: RevealGate
    session_factory: sessionmaker[Session] | None
    # The OpenBaoSecrets the tool functions take, or None when this deployment
    # has no vault address yet. Typed loosely so tests can pass a stand-in.
    secrets: Any | None
    # Runs the apply phases in the background. None when this deployment has no
    # datastore, since a job with nowhere to record itself is not a job.
    jobs: JobRunner | None = None


def services(request: Request) -> Services:
    return request.app.state.services


def audit_recorder(request: Request) -> AuditRecorder:
    """An auditor that writes outside the request's transaction, so a failed
    operation still leaves a row."""
    return AuditRecorder(services(request).session_factory)


def caller_email(request: Request) -> str:
    """The signed-in caller, or 401.

    A refused request is audited before it is refused. Without a row, the one
    pattern worth spotting (someone probing for a way in) is the one that leaves
    nothing behind. There is no identity to attribute it to, so it is recorded
    against a fixed name, with the header's NAME and whether one arrived, never
    what it said.
    """
    reader = services(request).identity
    try:
        return reader.email(request.headers)
    except NoIdentityError as exc:
        deciding = reader.assertion.header if reader.assertion else reader.header
        audit_recorder(request).record(
            user_email=UNIDENTIFIED,
            tool_name="identify",
            status=DENIED,
            args={"header": deciding, "present": bool(request.headers.getlist(deciding))},
            error=str(exc),
        )
        raise ApiError(401, "no identity") from exc


CallerEmail = Annotated[str, Depends(caller_email)]


def require_admin(request: Request, email: CallerEmail) -> str:
    """The caller, confirmed to be an admin of this deployment, or 403."""
    if not services(request).groups.is_admin(email):
        raise ApiError(403, f"{email} is not an admin of this deployment")
    return email


def declared_environment(request: Request) -> str | None:
    """The one environment this deployment serves, or None if it has not said.

    Handed to the tool functions, which do the refusing, so the agent surface
    and this one enforce it in the same place with the same sentence.
    """
    return services(request).settings.environment


def require_secrets(request: Request) -> Any:
    """The secret reader/writer, or 503 when no vault is configured."""
    secrets = services(request).secrets
    if secrets is None:
        raise ApiError(503, "OpenBao is not configured for this deployment")
    return secrets


def job_runner(request: Request) -> JobRunner:
    """The background job runner, or 503 when there is nowhere to record a job."""
    runner = services(request).jobs
    if runner is None:
        raise ApiError(503, "the datastore is not configured for this deployment")
    return runner


def db(request: Request) -> Iterator[Session]:
    """A datastore session for the request, or 503 when none is configured.

    The route owns the transaction and commits, matching how the MCP server
    wires the same tool functions: a failed call leaves nothing behind.
    """
    factory = services(request).session_factory
    if factory is None:
        raise ApiError(503, "the datastore is not configured for this deployment")
    with factory() as session:
        yield session


# The annotated forms the routes use. Identity comes first in every signature,
# so a request with no identity is refused before anything else is resolved.
AdminEmail = Annotated[str, Depends(require_admin)]
DbSession = Annotated[Session, Depends(db)]
Secrets = Annotated[Any, Depends(require_secrets)]
Auditor = Annotated[AuditRecorder, Depends(audit_recorder)]
Jobs = Annotated[JobRunner, Depends(job_runner)]
DeclaredEnvironment = Annotated[str | None, Depends(declared_environment)]
