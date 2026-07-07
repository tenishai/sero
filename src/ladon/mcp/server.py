"""The MCP server and its read-only tools (D12).

The tool *logic* lives in plain functions (``*_impl``) that take the read port
and a datastore session, gate access, audit the call, and return value-free
results. ``build_server`` registers them as FastMCP tools over OAuth 2.1 bearer
auth. Keeping the logic in functions (as the CLI keeps ``build_plan`` apart from
argparse) makes it testable with a fake reader and an in-memory session, with the
FastMCP wiring a thin shell.

Hygiene is built in: the read tools return masked key names only, never a value;
every call is written to the audit trail through the datastore repository; and
each call is gated by the allow list and per-namespace authorization.

Read-only slice: ``list_my_namespaces``, ``list_secrets``, ``get_secret``. The
write tools (create/update/request-namespace), which compose the apply layer,
follow in their own slice.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from sqlalchemy.orm import Session, sessionmaker

from ladon.datastore import repositories as repo
from ladon.mcp.ports import SecretReader

INSTRUCTIONS = """You are connected to a Ladon secrets platform.

You act as the signed-in user, and only on namespaces that user may access.

Hard rules, not preferences:

1. Never paste secret values, masked or otherwise, into your reply. get_secret
   returns masked key names so you can reason about which keys exist; the masked
   form is internal, never for display. Describe key NAMES to the user.
2. Do not fish. To answer "what do I have?", call list_secrets; for one path,
   call get_secret. Do not iterate get_secret across every path a listing
   returned.
3. Writes (in later tool slices) are dry-run first and require explicit
   confirmation, and placeholder values are refused.
"""


class AccessDeniedError(Exception):
    """The user is not permitted to perform the requested operation."""


# ── Tool logic (pure of FastMCP; testable with a fake reader + session) ──


def _audit(
    session: Session,
    *,
    user_email: str,
    tool_name: str,
    status: str,
    args: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    repo.append_call(
        session,
        user_email=user_email,
        method="tools/call",
        tool_name=tool_name,
        status=status,
        duration_ms=0,
        args=args,
        error=error,
    )


def _require_allowed(session: Session, user_email: str) -> None:
    if not repo.is_allowed(session, user_email):
        raise AccessDeniedError(f"{user_email} is not on the allow list")


def list_my_namespaces_impl(
    reader: SecretReader, session: Session, *, user_email: str
) -> dict[str, Any]:
    """The namespaces the user may read. Audited; allow-list gated."""
    _require_allowed(session, user_email)
    try:
        namespaces = reader.list_namespaces(user_email)
    except Exception as exc:
        _audit(
            session,
            user_email=user_email,
            tool_name="list_my_namespaces",
            status="error",
            error=str(exc),
        )
        raise
    _audit(session, user_email=user_email, tool_name="list_my_namespaces", status="ok")
    return {"namespaces": namespaces}


def list_secrets_impl(
    reader: SecretReader, session: Session, *, user_email: str, namespace: str
) -> dict[str, Any]:
    """Secret paths in a namespace (path + key count only). Audited; gated."""
    _require_allowed(session, user_email)
    _require_namespace(reader, user_email, namespace)
    args = {"namespace": namespace}
    try:
        paths = reader.list_secrets(user_email, namespace)
    except Exception as exc:
        _audit(
            session,
            user_email=user_email,
            tool_name="list_secrets",
            status="error",
            args=args,
            error=str(exc),
        )
        raise
    _audit(session, user_email=user_email, tool_name="list_secrets", status="ok", args=args)
    return {
        "namespace": namespace,
        "secrets": [{"path": p.path, "key_count": p.key_count} for p in paths],
    }


def get_secret_impl(
    reader: SecretReader,
    session: Session,
    *,
    user_email: str,
    namespace: str,
    path: str,
    env: str,
) -> dict[str, Any]:
    """One path's state in one environment, keys masked. Audited; gated.

    Returns masked key names only; a plaintext value never crosses this boundary.
    """
    _require_allowed(session, user_email)
    _require_namespace(reader, user_email, namespace)
    args = {"namespace": namespace, "path": path, "env": env}
    try:
        detail = reader.get_secret(user_email, namespace, path, env)
    except Exception as exc:
        _audit(
            session,
            user_email=user_email,
            tool_name="get_secret",
            status="error",
            args=args,
            error=str(exc),
        )
        raise
    _audit(session, user_email=user_email, tool_name="get_secret", status="ok", args=args)
    return {
        "namespace": detail.namespace,
        "path": detail.path,
        "env": detail.env,
        "exists": detail.exists,
        "version": detail.version,
        "key_count": detail.key_count,
        "masked_keys": detail.masked_keys,
    }


def _require_namespace(reader: SecretReader, user_email: str, namespace: str) -> None:
    """Reject a namespace the user cannot read, before touching secret data."""
    if namespace not in reader.list_namespaces(user_email):
        raise AccessDeniedError(f"{user_email} may not access namespace {namespace!r}")


# ── FastMCP wiring (thin) ────────────────────────────────────────────────


def build_server(
    reader: SecretReader,
    session_factory: sessionmaker[Session],
    *,
    name: str = "ladon",
    token_verifier: Any | None = None,
    auth: Any | None = None,
):
    """Build the FastMCP server with the read-only tools registered.

    ``reader`` is the secret read port; ``session_factory`` opens a datastore
    session per call (for the audit write and allow-list check). ``token_verifier``
    and ``auth`` are the OAuth 2.1 bearer machinery; when omitted (tests, local
    stdio) the server has no auth and the caller supplies the user identity.
    """
    kwargs: dict[str, Any] = {"instructions": INSTRUCTIONS, "stateless_http": True}
    if token_verifier is not None:
        kwargs["token_verifier"] = token_verifier
    if auth is not None:
        kwargs["auth"] = auth
    server = FastMCP(name, **kwargs)

    def _user_email(ctx: Context) -> str:
        # The bearer-validated token carries the user's email; see the token
        # verifier. Raised as AccessDenied if absent so tools fail closed.
        try:
            token = ctx.request_context.request.user.access_token  # type: ignore[union-attr]
            return token.user_email
        except Exception as exc:  # noqa: BLE001 - normalized to a closed failure
            raise AccessDeniedError(f"unauthenticated: {exc}") from exc

    read_only = ToolAnnotations(readOnlyHint=True, idempotentHint=True)

    @server.tool(annotations=read_only)
    def list_my_namespaces(ctx: Context) -> dict[str, Any]:
        """List the namespaces you may read."""
        with session_factory() as session:
            result = list_my_namespaces_impl(reader, session, user_email=_user_email(ctx))
            session.commit()
            return result

    @server.tool(annotations=read_only)
    def list_secrets(namespace: str, ctx: Context) -> dict[str, Any]:
        """List the secret paths in a namespace (names and key counts, no values)."""
        with session_factory() as session:
            result = list_secrets_impl(
                reader, session, user_email=_user_email(ctx), namespace=namespace
            )
            session.commit()
            return result

    @server.tool(annotations=read_only)
    def get_secret(namespace: str, path: str, env: str, ctx: Context) -> dict[str, Any]:
        """Inspect one secret path in one environment. Returns masked key names only."""
        with session_factory() as session:
            result = get_secret_impl(
                reader,
                session,
                user_email=_user_email(ctx),
                namespace=namespace,
                path=path,
                env=env,
            )
            session.commit()
            return result

    return server
