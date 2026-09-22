"""Render the baseline OpenBao auth methods (D3b).

The baseline ships two auth methods (docs/baseline.md): JWT for in-cluster
workloads (the External Secrets Operator authenticates with a projected service
account token) and OIDC for human operators. This module renders their
configuration from parameters into plain data: an ``AuthMethod`` carries the
mount-level config and its roles as the argument mappings the OpenBao API
expects. Rendering is pure; enabling the method and writing the config to OpenBao
is the SDK layer.

Secret hygiene: nothing here ever holds a secret value. The OIDC client secret is
carried as a *reference* (a name or path the apply layer resolves), stored under
an explicit ``oidc_client_secret_ref`` key, never as the secret itself. JWT
verification uses the provider's published keys (a discovery URL), so it needs no
secret at all.
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


def jwt_role(
    name: str,
    *,
    policies: list[str],
    bound_audiences: list[str] | None = None,
    bound_subject: str | None = None,
    user_claim: str = "sub",
    ttl: str = DEFAULT_TOKEN_TTL,
) -> AuthRole:
    """A JWT auth role binding a workload identity to OpenBao policies.

    The role must constrain who may use it: at least one of ``bound_subject``
    (e.g. a Kubernetes service account ``system:serviceaccount:ns:name``) or
    ``bound_audiences`` is required, so an unconstrained role cannot authenticate
    any token the provider issues.
    """
    if not policies:
        raise ConfigError(f"jwt role {name!r}: 'policies' must be non-empty")
    if not bound_subject and not bound_audiences:
        raise ConfigError(
            f"jwt role {name!r}: set 'bound_subject' or 'bound_audiences'; "
            "an unbound role authenticates any token"
        )
    data: dict = {
        "role_type": "jwt",
        "user_claim": user_claim,
        "token_policies": list(policies),
        "token_ttl": ttl,
        # Without this the role also carries the namespace's "default" policy,
        # whose grants are outside Ladon's control and tend to widen over time.
        # A scoped workload role must carry only the policies named here.
        "token_no_default_policy": True,
    }
    if bound_audiences:
        data["bound_audiences"] = list(bound_audiences)
    if bound_subject:
        data["bound_subject"] = bound_subject
    return AuthRole(name=name, data=data)


def jwt_auth(
    *,
    discovery_url: str,
    path: str = "jwt",
    bound_issuer: str | None = None,
    default_role: str | None = None,
    roles: list[AuthRole] | None = None,
) -> AuthMethod:
    """The JWT auth method for in-cluster workloads (the baseline ESO path).

    ``discovery_url`` is the OIDC discovery URL of the token issuer (the cluster
    in the Kubernetes case); OpenBao fetches the signing keys from it, so no
    secret is configured. Each cluster gets its own JWT mount (its own ``path``)
    bound to that cluster's issuer.

    ``bound_issuer`` defaults to ``discovery_url``: an empty bound issuer accepts
    any token the discovery keys verify, which is wider than the one issuer this
    mount exists for. Pass it explicitly only when the issuer claim differs from
    the discovery URL.
    """
    if not discovery_url:
        raise ConfigError("jwt auth requires a non-empty 'discovery_url'")
    config: dict = {
        "oidc_discovery_url": discovery_url,
        "bound_issuer": bound_issuer or discovery_url,
    }
    if default_role:
        config["default_role"] = default_role
    return AuthMethod(
        type="jwt",
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
