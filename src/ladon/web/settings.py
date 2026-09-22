"""Settings for the HTTP backend (D14, D15).

The backend reads the same deployment config the apply layer reads, plus a few
environment variables for what differs per process: where the built UI is, which
database, and where OpenBao is when the config does not say.

The config file holds names, paths, and references only. The OpenBao token is
read from the environment (``BAO_TOKEN``/``VAULT_TOKEN``, through
``ladon.apply.client``) and never from the file; a config that tries to carry one
is rejected rather than quietly honoured.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ladon.config.loader import DeploymentConfig, load_deployment_config
from ladon.config.teams import ConfigError
from ladon.web.assertion import (
    DEFAULT_ASSERTION_HEADER,
    DEFAULT_EMAIL_CLAIM,
    AssertionSettings,
)

# The proxy in front of the platform authenticates people and passes the result
# in this header. Configurable, because the header name is proxy-specific.
DEFAULT_IDENTITY_HEADER = "x-goog-authenticated-user-email"

# KV v2 mount inside a namespace, matching the baseline and DEFAULT_KV_MOUNT.
DEFAULT_KV_MOUNT = "kv"

CONFIG_PATH_ENV = "LADON_CONFIG"
UI_DIST_ENV = "LADON_UI_DIST"
# Address fallbacks, shared with the apply layer's client so one deployment does
# not need two spellings of the same address.
ADDRESS_ENV_VARS = ("BAO_ADDR", "VAULT_ADDR")


@dataclass(frozen=True)
class IdentitySettings:
    """How the caller's identity is established."""

    header: str = DEFAULT_IDENTITY_HEADER
    # The dev escape hatch (LADON_DEV_IDENTITY) is inert unless this is true, so
    # a deployed instance cannot be talked into trusting an environment variable.
    allow_dev_identity: bool = False
    # A signed assertion from the proxy. When configured it is required, it is
    # the identity, and ``header`` is not read: a claim gives way to a proof.
    assertion: AssertionSettings | None = None


@dataclass(frozen=True)
class AccessSettings:
    """Who may reach which namespaces, and who administers the deployment.

    ``members`` maps an email to namespaces directly; ``groups`` maps a group
    name to namespaces and ``group_members`` maps that group to emails. A caller
    gets the union. Emails and group names are stored lowercased so a config
    written with display casing still matches the header value.
    """

    admins: frozenset[str] = frozenset()
    members: dict[str, tuple[str, ...]] = field(default_factory=dict)
    groups: dict[str, tuple[str, ...]] = field(default_factory=dict)
    group_members: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class WebSettings:
    """Everything the backend needs to start, with no secret in it."""

    identity: IdentitySettings
    access: AccessSettings
    # The one environment this deployment serves. A request naming another is
    # refused: one Ladon, one vault, one environment. None means the deployment
    # has not said, in which case nothing is pinned.
    environment: str | None
    openbao_address: str | None
    kv_mount: str
    ui_dist: Path | None
    # Whether this deployment permits revealing a plaintext value at all. Off
    # unless a deployment says otherwise; see ladon.web.reveal.
    reveal_enabled: bool


def parse_identity(data: object) -> IdentitySettings:
    """Parse the ``identity:`` block."""
    if not data:
        return IdentitySettings()
    if not isinstance(data, dict):
        raise ConfigError("'identity' must be a mapping")
    header = data.get("header") or DEFAULT_IDENTITY_HEADER
    if not isinstance(header, str):
        raise ConfigError("'identity.header' must be a string")
    return IdentitySettings(
        header=header.lower(),
        allow_dev_identity=bool(data.get("allow_dev_identity", False)),
        assertion=parse_assertion(data.get("assertion")),
    )


def parse_assertion(data: object) -> AssertionSettings | None:
    """Parse the ``identity.assertion:`` block, or None when there is none.

    Every field that decides whether a token is acceptable is required. A
    half-configured assertion that verified a signature but not the audience
    would accept any token the same proxy signed for any other service.
    """
    if not data:
        return None
    if not isinstance(data, dict):
        raise ConfigError("'identity.assertion' must be a mapping")
    missing = [f for f in ("jwks_url", "audience", "issuer") if not data.get(f)]
    if missing:
        raise ConfigError(f"'identity.assertion' needs {', '.join(missing)}")
    header = data.get("header") or DEFAULT_ASSERTION_HEADER
    return AssertionSettings(
        jwks_url=str(data["jwks_url"]),
        audience=str(data["audience"]),
        issuer=str(data["issuer"]),
        header=str(header).lower(),
        email_claim=str(data.get("email_claim") or DEFAULT_EMAIL_CLAIM),
    )


def parse_access(data: object) -> AccessSettings:
    """Parse the ``access:`` block into the membership tables."""
    if not data:
        return AccessSettings()
    if not isinstance(data, dict):
        raise ConfigError("'access' must be a mapping")
    return AccessSettings(
        admins=frozenset(_names(data.get("admins"), "access.admins")),
        members=_name_lists(data.get("members"), "access.members"),
        groups=_name_lists(data.get("groups"), "access.groups"),
        group_members=_name_lists(data.get("group_members"), "access.group_members"),
    )


def parse_web_settings(config: DeploymentConfig, *, env: Mapping[str, str]) -> WebSettings:
    """Build the backend's settings from a parsed config and the environment."""
    deployment = config.settings
    openbao = deployment.get("openbao") or {}
    if not isinstance(openbao, dict):
        raise ConfigError("'deployment.openbao' must be a mapping")
    if "token" in openbao:
        raise ConfigError(
            "'deployment.openbao.token' is not read: the token comes from "
            f"{' or '.join(('BAO_TOKEN', 'VAULT_TOKEN'))} only"
        )

    reveal = deployment.get("reveal") or {}
    if not isinstance(reveal, dict):
        raise ConfigError("'deployment.reveal' must be a mapping")

    ui_dist = env.get(UI_DIST_ENV) or deployment.get("ui_dist")
    address = openbao.get("address") or _first(env, ADDRESS_ENV_VARS)

    environment = deployment.get("environment")
    return WebSettings(
        identity=parse_identity(config.identity),
        access=parse_access(config.access),
        environment=str(environment) if environment else None,
        openbao_address=address or None,
        kv_mount=deployment.get("kv_mount") or DEFAULT_KV_MOUNT,
        ui_dist=Path(ui_dist) if ui_dist else None,
        reveal_enabled=bool(reveal.get("enabled", False)),
    )


def load_web_settings(env: Mapping[str, str] | None = None) -> WebSettings:
    """Load settings for a running process.

    ``LADON_CONFIG`` points at the deployment config. It is optional: with no
    config the backend still starts and serves ``/healthz`` and ``/api/me``, with
    nobody holding any namespace, which is the honest answer for an instance that
    has not been configured yet.
    """
    env = os.environ if env is None else env
    path = env.get(CONFIG_PATH_ENV)
    config = load_deployment_config(path) if path else DeploymentConfig()
    return parse_web_settings(config, env=env)


def _first(env: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = env.get(name)
        if value:
            return value
    return None


def _names(data: object, where: str) -> list[str]:
    """A list of lowercased names, rejecting anything that is not a list."""
    if data is None:
        return []
    if not isinstance(data, list):
        raise ConfigError(f"'{where}' must be a list")
    return [str(item).strip().lower() for item in data if str(item).strip()]


def _name_lists(data: object, where: str) -> dict[str, tuple[str, ...]]:
    """A mapping of lowercased key to a tuple of names."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"'{where}' must be a mapping")
    return {
        str(key).strip().lower(): tuple(_names(value, f"{where}.{key}"))
        for key, value in data.items()
    }
