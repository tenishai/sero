"""The routes a signed-in person uses (D14).

Identity, namespaces, secrets, and namespace requests, exactly as
``ui/src/lib/api.ts`` calls them. The logic is not reimplemented here: each route
calls the same ``*_impl`` function the MCP tools call, so the allow list, the
namespace gate, placeholder refusal, the merge rules, and the audit row have one
implementation and cannot drift between the two surfaces.

Those functions are imported by their private names on purpose: they are the
service layer, and importing them is what keeps a second copy from appearing.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ladon.datastore import repositories as repo
from ladon.mcp.server import (
    _audit,
    _require_allowed,
    _require_namespace,
    create_secret_impl,
    get_secret_impl,
    list_my_namespaces_impl,
    list_secrets_impl,
    request_namespace_impl,
    update_secret_impl,
)
from ladon.web.deps import Auditor, CallerEmail, DbSession, Secrets, services
from ladon.web.serializers import namespace_request_row

router = APIRouter()


class CreateSecretBody(BaseModel):
    path: str
    env: str
    values: dict[str, str] = Field(default_factory=dict)
    # Dry-run by default, like every other write surface in Ladon.
    apply: bool = False


class UpdateSecretBody(BaseModel):
    env: str
    values: dict[str, str] = Field(default_factory=dict)
    remove_keys: list[str] = Field(default_factory=list)
    apply: bool = False


class NamespaceRequestBody(BaseModel):
    team_name: str
    environments: list[str] = Field(default_factory=list)
    members: list[str] = Field(default_factory=list)
    description: str | None = None


@router.get("/me")
def me(request: Request, email: CallerEmail) -> dict[str, Any]:
    """Who the proxy says you are, and what that gets you.

    Deliberately not behind the allow list: an admin bootstrapping a new
    deployment must be able to see themselves before anyone is on it.
    """
    svc = services(request)
    return {
        "email": email,
        "namespaces": svc.groups.namespaces_for(email),
        "is_admin": svc.groups.is_admin(email),
    }


@router.get("/namespaces")
def list_namespaces(
    email: CallerEmail,
    session: DbSession,
    secrets: Secrets,
) -> dict[str, Any]:
    result = list_my_namespaces_impl(secrets, session, user_email=email)
    session.commit()
    return result


@router.get("/namespaces/{namespace}/secrets")
def list_secrets(
    namespace: str,
    email: CallerEmail,
    session: DbSession,
    secrets: Secrets,
) -> dict[str, Any]:
    result = list_secrets_impl(secrets, session, user_email=email, namespace=namespace)
    session.commit()
    return result


# Registered before the detail route: the path converter is greedy, so the more
# specific suffix has to be offered to the router first.
@router.get("/namespaces/{namespace}/secrets/{secret_path:path}/reveal")
def reveal_secret_value(
    namespace: str,
    secret_path: str,
    env: str,
    key: str,
    request: Request,
    email: CallerEmail,
    session: DbSession,
    secrets: Secrets,
    audit: Auditor,
) -> dict[str, str]:
    """The one route that returns a plaintext value.

    Three gates stand in front of it: the deployment's reveal gate (refusing by
    default), the allow list, and namespace membership. Whichever refuses, the
    attempt is audited as ``denied`` with the reason. The audit row records which
    key was asked for, never what it held.
    """
    args = {"namespace": namespace, "path": secret_path, "env": env, "key_name": key}
    with audit.guard("reveal_value", email, args, session=session):
        services(request).reveal_gate.require(email)
        _require_allowed(session, email)
        _require_namespace(secrets, email, namespace)
        value = secrets.reveal_value(email, namespace, secret_path, env, key)
    _audit(session, user_email=email, tool_name="reveal_value", status="ok", args=args)
    session.commit()
    return {"value": value}


@router.get("/namespaces/{namespace}/secrets/{secret_path:path}")
def get_secret(
    namespace: str,
    secret_path: str,
    env: str,
    email: CallerEmail,
    session: DbSession,
    secrets: Secrets,
) -> dict[str, Any]:
    result = get_secret_impl(
        secrets, session, user_email=email, namespace=namespace, path=secret_path, env=env
    )
    session.commit()
    return result


@router.post("/namespaces/{namespace}/secrets")
def create_secret(
    namespace: str,
    body: CreateSecretBody,
    email: CallerEmail,
    session: DbSession,
    secrets: Secrets,
    audit: Auditor,
) -> dict[str, Any]:
    args = {"namespace": namespace, "path": body.path, "env": body.env}
    with audit.guard("create_secret", email, args, session=session):
        result = create_secret_impl(
            secrets,
            secrets,
            session,
            user_email=email,
            namespace=namespace,
            path=body.path,
            env=body.env,
            values=body.values,
            apply=body.apply,
        )
    session.commit()
    return result


@router.patch("/namespaces/{namespace}/secrets/{secret_path:path}")
def update_secret(
    namespace: str,
    secret_path: str,
    body: UpdateSecretBody,
    email: CallerEmail,
    session: DbSession,
    secrets: Secrets,
    audit: Auditor,
) -> dict[str, Any]:
    args = {"namespace": namespace, "path": secret_path, "env": body.env}
    with audit.guard("update_secret", email, args, session=session):
        result = update_secret_impl(
            secrets,
            secrets,
            session,
            user_email=email,
            namespace=namespace,
            path=secret_path,
            env=body.env,
            values=body.values,
            remove_keys=body.remove_keys,
            apply=body.apply,
        )
    session.commit()
    return result


@router.post("/namespace-requests")
def request_namespace(
    body: NamespaceRequestBody,
    email: CallerEmail,
    session: DbSession,
) -> dict[str, Any]:
    result = request_namespace_impl(
        session,
        user_email=email,
        team_name=body.team_name,
        environments=body.environments,
        members=body.members,
        description=body.description,
    )
    session.commit()
    return result


@router.get("/namespace-requests")
def list_my_namespace_requests(
    email: CallerEmail,
    session: DbSession,
) -> dict[str, Any]:
    """The caller's own requests. The admin queue is a separate, gated route."""
    rows = repo.list_namespace_requests(session)
    mine = [r for r in rows if r.requester_email == email]
    return {"requests": [namespace_request_row(r) for r in mine]}
