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
from ladon.hygiene import WriteRefused, assert_not_placeholder
from ladon.mcp.ports import SecretReader, SecretWriter, require_non_empty_merge

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


class WrongEnvironment(ValueError):
    """The call named an environment this deployment does not serve."""


def require_environment(env: str, declared: str | None) -> None:
    """Refuse a call for an environment this deployment does not serve.

    One Ladon serves one OpenBao, whose KV holds one set of paths, so a call
    labelled for another environment would read and write the same secrets under
    a name that says otherwise. Shared by the HTTP and the agent surface so both
    refuse with the same sentence; ``declared`` of None pins nothing, which is
    the honest behaviour for a deployment that has not said.
    """
    if declared and env != declared:
        raise WrongEnvironment(f"this deployment serves environment {declared!r}")


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
    deployment_environment: str | None = None,
) -> dict[str, Any]:
    """One path's state in one environment, keys masked. Audited; gated.

    Returns masked key names only; a plaintext value never crosses this boundary.
    """
    _require_allowed(session, user_email)
    _require_namespace(reader, user_email, namespace)
    require_environment(env, deployment_environment)
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


def _reject_placeholders(values: dict[str, str]) -> None:
    """Refuse placeholder secret values before any write. Never names the value."""
    for key, value in values.items():
        assert_not_placeholder(value, key=key)


# The HTTP surface addresses a secret's plaintext at <path>/reveal, so a path
# that ends this way would be unreachable through its own detail route. Refused
# at creation, where it costs nothing, rather than discovered later.
RESERVED_PATH_SUFFIX = "/reveal"


def _reject_reserved_path(path: str) -> None:
    """Refuse a secret path the read surfaces could not address."""
    if path.rstrip("/").endswith(RESERVED_PATH_SUFFIX):
        raise WriteRefused(
            f"{RESERVED_PATH_SUFFIX!r} is a reserved path suffix; choose another name"
        )


def _write_summary(
    *,
    namespace: str,
    path: str,
    env: str,
    created: bool,
    written_keys: list[str],
    removed_keys: list[str],
    applied: bool,
) -> dict[str, Any]:
    """A value-free description of a write, for a dry-run or an applied result."""
    return {
        "applied": applied,
        "action": "create" if created else "update",
        "namespace": namespace,
        "path": path,
        "env": env,
        "written_keys": sorted(written_keys),
        "removed_keys": sorted(removed_keys),
    }


def create_secret_impl(
    reader: SecretReader,
    writer: SecretWriter,
    session: Session,
    *,
    user_email: str,
    namespace: str,
    path: str,
    env: str,
    values: dict[str, str],
    apply: bool = False,
    deployment_environment: str | None = None,
) -> dict[str, Any]:
    """Create a secret path for a signed-in person. Dry-run unless ``apply=True``.

    Gated by the allow list and namespace authorization; the write itself, and
    every refusal in it, is :func:`create_secret_write`.
    """
    _require_allowed(session, user_email)
    _require_namespace(reader, user_email, namespace)
    return create_secret_write(
        writer,
        session,
        user_email=user_email,
        namespace=namespace,
        path=path,
        env=env,
        values=values,
        apply=apply,
        deployment_environment=deployment_environment,
    )


def create_secret_write(
    writer: SecretWriter,
    session: Session,
    *,
    user_email: str,
    namespace: str,
    path: str,
    env: str,
    values: dict[str, str],
    apply: bool = False,
    deployment_environment: str | None = None,
    tool_name: str = "create_secret",
    audit_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a secret path, for a caller already authorized to write here.

    Everything a write refuses lives here: an environment this deployment does
    not serve, a reserved path, a placeholder value. A dry-run returns the
    value-free plan and writes nothing. The audit row records key names only
    (args are redacted before storage).

    How the caller was authorized is the caller's business: the human surfaces
    gate on the allow list and namespace membership before calling, while the
    machine lane gates on the namespaces its config pins to one subject. Both
    reach the same rules, which is what keeps them from drifting. ``tool_name``
    and ``audit_extra`` let a surface say which lane wrote and on whose token.
    """
    require_environment(env, deployment_environment)
    _reject_reserved_path(path)
    _reject_placeholders(values)
    audit_args = {
        "namespace": namespace,
        "path": path,
        "env": env,
        "data": values,
        **(audit_extra or {}),
    }

    if not apply:
        _audit(
            session,
            user_email=user_email,
            tool_name=tool_name,
            status="dry-run",
            args=audit_args,
        )
        return _write_summary(
            namespace=namespace,
            path=path,
            env=env,
            created=True,
            written_keys=list(values),
            removed_keys=[],
            applied=False,
        )
    try:
        result = writer.create_secret(user_email, namespace, path, env, values)
    except Exception as exc:
        _audit(
            session,
            user_email=user_email,
            tool_name=tool_name,
            status="error",
            args=audit_args,
            error=str(exc),
        )
        raise
    _audit(session, user_email=user_email, tool_name=tool_name, status="ok", args=audit_args)
    return _write_summary(
        namespace=namespace,
        path=path,
        env=env,
        created=result.created,
        written_keys=result.written_keys,
        removed_keys=result.removed_keys,
        applied=True,
    )


def update_secret_impl(
    reader: SecretReader,
    writer: SecretWriter,
    session: Session,
    *,
    user_email: str,
    namespace: str,
    path: str,
    env: str,
    values: dict[str, str] | None = None,
    remove_keys: list[str] | None = None,
    apply: bool = False,
    deployment_environment: str | None = None,
) -> dict[str, Any]:
    """Merge into a path for a signed-in person. Dry-run unless ``apply=True``.

    Gated by the allow list and namespace authorization; the merge itself, and
    every refusal in it, is :func:`update_secret_write`.
    """
    _require_allowed(session, user_email)
    _require_namespace(reader, user_email, namespace)
    return update_secret_write(
        reader,
        writer,
        session,
        user_email=user_email,
        namespace=namespace,
        path=path,
        env=env,
        values=values,
        remove_keys=remove_keys,
        apply=apply,
        deployment_environment=deployment_environment,
    )


def update_secret_write(
    reader: SecretReader,
    writer: SecretWriter,
    session: Session,
    *,
    user_email: str,
    namespace: str,
    path: str,
    env: str,
    values: dict[str, str] | None = None,
    remove_keys: list[str] | None = None,
    apply: bool = False,
    deployment_environment: str | None = None,
    tool_name: str = "update_secret",
    audit_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge ``values`` into a path and delete ``remove_keys``, already authorized.

    Merges: only the named keys change; other existing keys are untouched.
    Placeholder-refusing like create. A key named in both ``values`` and
    ``remove_keys`` is refused. A dry-run writes nothing. Emptying a path is
    refused by the writer, which is where the merged result is known.

    The counterpart to :func:`create_secret_write`: one copy of the merge rules,
    reached by the human surfaces and by the machine lane alike.
    """
    values = values or {}
    remove_keys = remove_keys or []
    require_environment(env, deployment_environment)
    _reject_reserved_path(path)
    _reject_placeholders(values)
    if not values and not remove_keys:
        raise WriteRefused("update_secret needs at least one of 'values' or 'remove_keys'")
    # A key on both sides is ambiguous: written then deleted, or the reverse,
    # depending on an order the caller cannot see. Refuse instead of guessing.
    both = sorted(set(values) & set(remove_keys))
    if both:
        raise WriteRefused(f"keys named in both 'values' and 'remove_keys': {', '.join(both)}")
    audit_args = {
        "namespace": namespace,
        "path": path,
        "env": env,
        "data": values,
        "remove_keys": remove_keys,
        **(audit_extra or {}),
    }

    if not apply:
        # Preview the writer's refusal too: a dry-run that says "this is what
        # would happen" and then fails on apply is worse than no dry-run. The
        # reader returns masked keys, so only key names are needed here.
        current = reader.get_secret(user_email, namespace, path, env)
        require_non_empty_merge(
            namespace=namespace,
            path=path,
            current_keys=current.masked_keys if current.exists else {},
            values=values,
            remove_keys=remove_keys,
        )
        _audit(
            session,
            user_email=user_email,
            tool_name=tool_name,
            status="dry-run",
            args=audit_args,
        )
        return _write_summary(
            namespace=namespace,
            path=path,
            env=env,
            created=False,
            written_keys=list(values),
            removed_keys=remove_keys,
            applied=False,
        )
    try:
        result = writer.update_secret(user_email, namespace, path, env, values, remove_keys)
    except Exception as exc:
        _audit(
            session,
            user_email=user_email,
            tool_name=tool_name,
            status="error",
            args=audit_args,
            error=str(exc),
        )
        raise
    _audit(session, user_email=user_email, tool_name=tool_name, status="ok", args=audit_args)
    return _write_summary(
        namespace=namespace,
        path=path,
        env=env,
        created=result.created,
        written_keys=result.written_keys,
        removed_keys=result.removed_keys,
        applied=True,
    )


def request_namespace_impl(
    session: Session,
    *,
    user_email: str,
    team_name: str,
    environments: list[str],
    members: list[str],
    description: str | None = None,
) -> dict[str, Any]:
    """Open a namespace-request workflow row. Allow-list gated; audited.

    This writes only to Ladon's own datastore (the request queue), not to
    OpenBao, so it has no dry-run: it records a request an admin later acts on.
    """
    _require_allowed(session, user_email)
    if not environments:
        raise ValueError("request_namespace needs at least one environment")
    row = repo.create_namespace_request(
        session,
        requester_email=user_email,
        team_name=team_name,
        environments=environments,
        members=members or [user_email],
        description=description,
    )
    _audit(
        session,
        user_email=user_email,
        tool_name="request_namespace",
        status="ok",
        args={"team_name": team_name, "environments": environments},
    )
    return {"request_id": row.id, "status": row.status, "team_name": team_name}


# ── FastMCP wiring (thin) ────────────────────────────────────────────────


def build_server(
    reader: SecretReader,
    session_factory: sessionmaker[Session],
    *,
    writer: SecretWriter | None = None,
    name: str = "ladon",
    token_verifier: Any | None = None,
    auth: Any | None = None,
    environment: str | None = None,
):
    """Build the FastMCP server with the read and write tools registered.

    ``reader`` is the secret read port; ``writer`` is the write port (when
    omitted, only the read tools and request_namespace are registered).
    ``session_factory`` opens a datastore session per call (for the audit write
    and allow-list check). ``token_verifier`` and ``auth`` are the OAuth 2.1
    bearer machinery; when omitted (tests, local stdio) the server has no auth and
    the caller supplies the user identity. ``environment`` is the one environment
    this deployment serves; a tool call naming another is refused, exactly as on
    the HTTP surface.
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
                deployment_environment=environment,
            )
            session.commit()
            return result

    # A namespace request writes only to Ladon's own request queue, so it is
    # available whether or not a secret writer is configured.
    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False))
    def request_namespace(
        team_name: str,
        environments: list[str],
        ctx: Context,
        members: list[str] | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Request a new team namespace. Records a request for an admin to action."""
        with session_factory() as session:
            result = request_namespace_impl(
                session,
                user_email=_user_email(ctx),
                team_name=team_name,
                environments=environments,
                members=members or [],
                description=description,
            )
            session.commit()
            return result

    if writer is not None:
        _register_write_tools(
            server, reader, writer, session_factory, _user_email, environment=environment
        )

    return server


def _register_write_tools(
    server, reader, writer, session_factory, _user_email, *, environment=None
):
    """Register the secret-write tools. Split out so build_server stays readable."""
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)

    @server.tool(annotations=write)
    def create_secret(
        namespace: str,
        path: str,
        env: str,
        values: dict[str, str],
        ctx: Context,
        apply: bool = False,
    ) -> dict[str, Any]:
        """Create a secret path. Dry-run unless apply=True. Never echoes values."""
        with session_factory() as session:
            result = create_secret_impl(
                reader,
                writer,
                session,
                user_email=_user_email(ctx),
                namespace=namespace,
                path=path,
                env=env,
                values=values,
                apply=apply,
                deployment_environment=environment,
            )
            session.commit()
            return result

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)
    )
    def update_secret(
        namespace: str,
        path: str,
        env: str,
        ctx: Context,
        values: dict[str, str] | None = None,
        remove_keys: list[str] | None = None,
        apply: bool = False,
    ) -> dict[str, Any]:
        """Merge values into a path and delete remove_keys. Dry-run unless apply=True."""
        with session_factory() as session:
            result = update_secret_impl(
                reader,
                writer,
                session,
                user_email=_user_email(ctx),
                namespace=namespace,
                path=path,
                env=env,
                values=values,
                remove_keys=remove_keys,
                apply=apply,
                deployment_environment=environment,
            )
            session.commit()
            return result
