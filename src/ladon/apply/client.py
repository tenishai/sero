"""Construct and authenticate an OpenBao client for the apply layer (D3b).

The executor (``ladon.apply.executor.apply_plan``) takes an authenticated
``hvac.Client`` and never builds one. This module builds one: it turns a
value-free :class:`BaoConnection` (address, namespace, TLS) plus a credential
into a pooled, authenticated client, optionally verifying the connection before
handing it back.

Credentials are a small typed set: a static token, AppRole, or JWT login. Each
obtains a token via hvac and sets it on the client; secret material (the token,
an AppRole secret id, a JWT) is refused if it looks like a placeholder and is
never logged. Connection settings carry no secret, so a :class:`BaoConnection`
is safe to print or store.

Brand-neutral by design: the token can be passed explicitly or read from a
generic environment variable. Sourcing it from a specific cloud secret manager
is a separate adapter's job, not baked in here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import hvac
import requests
from requests.adapters import HTTPAdapter

from ladon.hygiene import PlaceholderSecretError, assert_not_placeholder

# Generic, vendor-neutral environment variables for the token fallback. Both the
# OpenBao and upstream names are accepted so existing tooling works unchanged.
TOKEN_ENV_VARS = ("BAO_TOKEN", "VAULT_TOKEN")
ADDRESS_ENV_VARS = ("BAO_ADDR", "VAULT_ADDR")

DEFAULT_TIMEOUT = 30


class BaoConnectionError(RuntimeError):
    """The client could not be built, authenticated, or reached."""


@dataclass(frozen=True)
class BaoConnection:
    """Where and how to reach an OpenBao, with no secret material.

    ``tls_verify`` is either a bool (verify against the system trust store, or
    disable verification) or a path to a CA bundle. ``namespace`` scopes the
    client to one OpenBao namespace when set.
    """

    address: str
    namespace: str | None = None
    tls_verify: bool | str = True
    timeout: int = DEFAULT_TIMEOUT

    @classmethod
    def from_env(
        cls,
        *,
        address: str | None = None,
        namespace: str | None = None,
        tls_verify: bool | str = True,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> BaoConnection:
        """Build a connection, reading the address from the environment if absent."""
        resolved = address or _first_env(ADDRESS_ENV_VARS)
        if not resolved:
            raise BaoConnectionError(
                f"no OpenBao address: pass one or set {' or '.join(ADDRESS_ENV_VARS)}"
            )
        return cls(
            address=resolved,
            namespace=namespace,
            tls_verify=tls_verify,
            timeout=timeout,
        )


@dataclass(frozen=True)
class TokenAuth:
    """Authenticate with an existing OpenBao token."""

    token: str

    def authenticate(self, client: hvac.Client) -> None:
        assert_not_placeholder(self.token, key="token")
        client.token = self.token

    @classmethod
    def from_env(cls) -> TokenAuth:
        """Read the token from a generic environment variable."""
        token = _first_env(TOKEN_ENV_VARS)
        if not token:
            raise BaoConnectionError(f"no token: set {' or '.join(TOKEN_ENV_VARS)}")
        return cls(token=token)


@dataclass(frozen=True)
class AppRoleAuth:
    """Authenticate via AppRole login, exchanging a role id and secret id for a token."""

    role_id: str
    secret_id: str
    mount_point: str = "approle"

    def authenticate(self, client: hvac.Client) -> None:
        assert_not_placeholder(self.secret_id, key="secret_id")
        # use_token=True sets the issued client token on the client.
        client.auth.approle.login(
            role_id=self.role_id,
            secret_id=self.secret_id,
            mount_point=self.mount_point,
        )


@dataclass(frozen=True)
class JwtAuth:
    """Authenticate via JWT login against a role on a JWT auth mount."""

    role: str
    jwt: str
    path: str | None = None

    def authenticate(self, client: hvac.Client) -> None:
        assert_not_placeholder(self.jwt, key="jwt")
        client.auth.jwt.jwt_login(role=self.role, jwt=self.jwt, path=self.path)


# A credential is anything that can set a valid token on a client.
Credential = TokenAuth | AppRoleAuth | JwtAuth


def _first_env(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _build_client(conn: BaoConnection) -> hvac.Client:
    # A pooled session keeps TLS connections hot across the many small writes a
    # plan makes, instead of a fresh handshake per request. Retries are left to
    # the caller's plan semantics, so the adapter does not retry on its own.
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=16, max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return hvac.Client(
        url=conn.address,
        namespace=conn.namespace,
        verify=conn.tls_verify,
        timeout=conn.timeout,
        session=session,
    )


def _verify(client: hvac.Client) -> None:
    """Confirm the client is authenticated and the server is reachable.

    Read-only: it checks token validity and the server health, and never writes.
    Errors are surfaced without the token or any payload.
    """
    try:
        authed = client.is_authenticated()
    except Exception as exc:  # noqa: BLE001 - normalized to BaoConnectionError
        raise BaoConnectionError(f"could not reach OpenBao at {client.url}: {exc}") from exc
    if not authed:
        raise BaoConnectionError("OpenBao rejected the credential (not authenticated)")
    try:
        client.sys.read_health_status(method="GET")
    except Exception as exc:  # noqa: BLE001 - normalized to BaoConnectionError
        raise BaoConnectionError(f"OpenBao health check failed: {exc}") from exc


def connect(conn: BaoConnection, credential: Credential, *, check: bool = True) -> hvac.Client:
    """Build a pooled client, authenticate it, and (by default) verify it.

    The credential sets a token on the client (directly, or via an AppRole/JWT
    login). When ``check`` is true the connection is verified read-only before
    the client is returned, so a bad address or credential fails fast rather than
    surfacing later mid-apply. Pass ``check=False`` to skip the live check (for
    example when constructing against a server that is reachable but sealed).
    """
    client = _build_client(conn)
    try:
        credential.authenticate(client)
    except BaoConnectionError, PlaceholderSecretError:
        # A placeholder refusal is a hygiene failure, not a connection failure;
        # let it propagate unchanged so callers can tell them apart.
        raise
    except Exception as exc:  # noqa: BLE001 - normalized; never include the secret
        raise BaoConnectionError(f"authentication failed: {exc}") from exc
    if check:
        _verify(client)
    return client
