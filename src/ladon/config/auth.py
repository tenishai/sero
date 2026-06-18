"""Render the baseline OpenBao auth methods (D3b).

The baseline ships two auth methods (docs/baseline.md): Kubernetes for in-cluster
workloads and OIDC for human operators. This module renders their configuration
from parameters into plain data: an ``AuthMethod`` carries the mount-level config
and its roles as the argument mappings the OpenBao API expects. Rendering is
pure; enabling the method and writing the config to OpenBao is the SDK layer.

Secret hygiene: nothing here ever holds a secret value. The OIDC client secret is
carried as a *reference* (a name or path the apply layer resolves), stored under
an explicit ``oidc_client_secret_ref`` key, never as the secret itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ladon.config.teams import ConfigError

# Sensible generic token lifetime for issued tokens; deployments may override.
DEFAULT_TOKEN_TTL = "1h"


@dataclass(frozen=True)
class AuthRole:
    """One role on an auth method: its name and the argument mapping for it."""

    name: str
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AuthMethod:
    """An auth method to enable: its type, mount path, config, and roles.

    ``config`` is the mapping written to ``auth/<path>/config``; each role's
    ``data`` is written to ``auth/<path>/role/<name>``. None of it carries a
    secret value.
    """

    type: str
    path: str
    config: dict = field(default_factory=dict)
    roles: tuple[AuthRole, ...] = ()


def kubernetes_role(
    name: str,
    *,
    service_accounts: list[str],
    namespaces: list[str],
    policies: list[str],
    ttl: str = DEFAULT_TOKEN_TTL,
) -> AuthRole:
    """A Kubernetes auth role binding service accounts to OpenBao policies.

    Binds the named Kubernetes service accounts in the named Kubernetes
    namespaces to the given OpenBao policies. All bindings are required; an
    unbound role would authenticate any workload.
    """
    if not service_accounts:
        raise ConfigError(f"kubernetes role {name!r}: 'service_accounts' must be non-empty")
    if not namespaces:
        raise ConfigError(f"kubernetes role {name!r}: 'namespaces' must be non-empty")
    if not policies:
        raise ConfigError(f"kubernetes role {name!r}: 'policies' must be non-empty")
    return AuthRole(
        name=name,
        data={
            "bound_service_account_names": list(service_accounts),
            "bound_service_account_namespaces": list(namespaces),
            "token_policies": list(policies),
            "token_ttl": ttl,
        },
    )


def kubernetes_auth(
    *,
    host: str,
    path: str = "kubernetes",
    ca_cert_ref: str | None = None,
    roles: list[AuthRole] | None = None,
) -> AuthMethod:
    """The Kubernetes auth method for in-cluster workloads.

    ``host`` is the API server URL. ``ca_cert_ref`` is an optional reference to
    the cluster CA (a name or path the apply layer resolves), never the cert
    itself; omit it to use the in-cluster service-account CA. The token reviewer
    JWT is intentionally not set here: the baseline uses OpenBao's own pod
    service account as the reviewer, configured at apply time.
    """
    if not host:
        raise ConfigError("kubernetes auth requires a non-empty 'host'")
    config: dict = {"kubernetes_host": host}
    if ca_cert_ref:
        config["kubernetes_ca_cert_ref"] = ca_cert_ref
    return AuthMethod(
        type="kubernetes",
        path=path,
        config=config,
        roles=tuple(roles or ()),
    )


def oidc_role(
    name: str,
    *,
    policies: list[str],
    allowed_redirect_uris: list[str],
    user_claim: str = "sub",
    groups_claim: str = "groups",
    bound_groups: list[str] | None = None,
    ttl: str = DEFAULT_TOKEN_TTL,
) -> AuthRole:
    """An OIDC role mapping authenticated operators to OpenBao policies.

    ``allowed_redirect_uris`` is required: OIDC login fails closed without it.
    ``bound_groups`` restricts the role to operators in those identity-provider
    groups; an empty value means the role is not group-restricted.
    """
    if not policies:
        raise ConfigError(f"oidc role {name!r}: 'policies' must be non-empty")
    if not allowed_redirect_uris:
        raise ConfigError(f"oidc role {name!r}: 'allowed_redirect_uris' must be non-empty")
    data: dict = {
        "user_claim": user_claim,
        "groups_claim": groups_claim,
        "allowed_redirect_uris": list(allowed_redirect_uris),
        "token_policies": list(policies),
        "token_ttl": ttl,
    }
    if bound_groups:
        data["bound_claims"] = {groups_claim: list(bound_groups)}
    return AuthRole(name=name, data=data)


def oidc_auth(
    *,
    discovery_url: str,
    client_id: str,
    client_secret_ref: str,
    path: str = "oidc",
    default_role: str | None = None,
    roles: list[AuthRole] | None = None,
) -> AuthMethod:
    """The OIDC auth method for human operators.

    ``discovery_url`` and ``client_id`` are public configuration.
    ``client_secret_ref`` is a *reference* the apply layer resolves into the
    actual client secret; the secret value never passes through Ladon's
    rendering. The reference is stored under ``oidc_client_secret_ref`` so the
    boundary is explicit.
    """
    if not discovery_url:
        raise ConfigError("oidc auth requires a non-empty 'discovery_url'")
    if not client_id:
        raise ConfigError("oidc auth requires a non-empty 'client_id'")
    if not client_secret_ref:
        raise ConfigError("oidc auth requires a non-empty 'client_secret_ref'")
    config: dict = {
        "oidc_discovery_url": discovery_url,
        "oidc_client_id": client_id,
        "oidc_client_secret_ref": client_secret_ref,
    }
    if default_role:
        config["default_role"] = default_role
    return AuthMethod(
        type="oidc",
        path=path,
        config=config,
        roles=tuple(roles or ()),
    )
