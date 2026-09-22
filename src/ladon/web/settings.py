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
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ladon.config.loader import (
    DeploymentConfig,
    load_deployment_config,
    load_deployment_config_text,
)
from ladon.config.teams import ConfigError
from ladon.web.assertion import (
    DEFAULT_ASSERTION_HEADER,
    DEFAULT_EMAIL_CLAIM,
    AssertionSettings,
)

# What a machine caller's token must carry (D16). The human lane's three, plus
# ``iat`` (so a token's age is knowable) and ``sub`` (the lane authorizes on it).
SERVICE_REQUIRED_CLAIMS = ("exp", "iat", "iss", "aud", "sub")

# Calls per minute one machine caller may make across the whole lane, unless a
# deployment says otherwise. Generous for provisioning a handful of secrets,
# small enough that a stuck loop stops rather than filling the vault.
DEFAULT_SERVICE_RATE_LIMIT = 60

# Kubernetes DNS-1123 label rules, which a namespace name obeys.
_K8S_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_K8S_NAME_MAX = 63

# The proxy in front of the platform authenticates people and passes the result
# in this header. Configurable, because the header name is proxy-specific.
DEFAULT_IDENTITY_HEADER = "x-goog-authenticated-user-email"

# KV v2 mount inside a namespace, matching the baseline and DEFAULT_KV_MOUNT.
DEFAULT_KV_MOUNT = "kv"

CONFIG_PATH_ENV = "LADON_CONFIG"
# The same document inline, for a deployment whose config arrives as an
# environment variable rather than a mounted file. See load_web_settings.
CONFIG_INLINE_ENV = "LADON_CONFIG_YAML"
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
class ServiceCaller:
    """One machine permitted to write on a person's behalf (D16).

    Everything that decides what this caller can do is pinned here and nowhere
    else: the token must come from ``assertion``'s issuer, carry its audience,
    and name ``subject``; the write must land in one of ``namespaces``; a
    cluster object must land in the application's own Kubernetes namespace or
    one of ``k8s_namespaces``. The acting email the call carries is attribution,
    never authorization, so no field here depends on it.
    """

    name: str
    subject: str
    namespaces: tuple[str, ...]
    assertion: AssertionSettings
    # Kubernetes namespaces this caller may place an ExternalSecret in, beyond
    # the application's own name. Empty is the safe default and the common case:
    # one application, one namespace named after it.
    k8s_namespaces: tuple[str, ...] = ()
    rate_limit_per_minute: int = DEFAULT_SERVICE_RATE_LIMIT

    @property
    def issuer(self) -> str:
        return self.assertion.issuer

    @property
    def audience(self) -> str:
        return self.assertion.audience


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
    # The machines permitted to write on a person's behalf (D16). Empty by
    # default, which closes the machine lane: a deployment opts in by naming
    # callers, never by forgetting to.
    service_callers: tuple[ServiceCaller, ...] = ()


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
    jwks_url = str(data["jwks_url"])
    if not jwks_url.lower().startswith("https://"):
        # These keys decide who everybody is. Over plain HTTP anyone on the path
        # chooses the signer, which makes verifying the assertion theatre.
        raise ConfigError("'identity.assertion.jwks_url' must be an https URL")
    header = data.get("header") or DEFAULT_ASSERTION_HEADER
    return AssertionSettings(
        jwks_url=jwks_url,
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


def parse_service_callers(data: object, *, human_audience: str | None) -> tuple[ServiceCaller, ...]:
    """Parse the ``service_callers:`` list, or an empty tuple when there is none.

    Every field is required, because every one of them is a boundary: drop the
    issuer or the audience and the lane accepts a token minted for somewhere
    else; drop the subject and any workload that issuer trusts becomes this
    caller; drop the namespaces and one machine reaches the whole vault.

    ``human_audience`` is the audience the human assertion carries. A caller
    claiming the same one is refused outright: the two lanes would then accept
    each other's tokens, and a person's browser credential would be a machine
    credential.
    """
    if not data:
        return ()
    if not isinstance(data, list):
        raise ConfigError("'service_callers' must be a list")
    callers = [_service_caller(item, index, human_audience) for index, item in enumerate(data)]
    _reject_ambiguous_callers(callers)
    return tuple(callers)


def _service_caller(data: object, index: int, human_audience: str | None) -> ServiceCaller:
    where = f"service_callers[{index}]"
    if not isinstance(data, dict):
        raise ConfigError(f"'{where}' must be a mapping")
    missing = [
        field_name
        for field_name in ("name", "issuer", "jwks_url", "audience", "subject", "namespaces")
        if not data.get(field_name)
    ]
    if missing:
        raise ConfigError(f"'{where}' needs {', '.join(missing)}")

    jwks_url = str(data["jwks_url"])
    if not jwks_url.lower().startswith("https://"):
        # These keys decide which machine is which. Over plain HTTP anyone on
        # the path chooses the signer, which makes verifying the token theatre.
        raise ConfigError(f"'{where}.jwks_url' must be an https URL")

    audience = str(data["audience"])
    if human_audience and audience == human_audience:
        # Separate lanes are the point of D16. One shared audience and a
        # person's proxy assertion is replayable as a machine credential.
        raise ConfigError(
            f"'{where}.audience' is the same audience the human assertion "
            "carries; give the machine lane an audience of its own"
        )

    if not data.get("acting_email_required", True):
        # The lane exists to write on a person's behalf, and the row that says
        # whose behalf is the only trace a machine write leaves of a human.
        raise ConfigError(
            f"'{where}.acting_email_required' cannot be false: the machine lane "
            "always records the person it acts for"
        )

    namespaces = _names(data["namespaces"], f"{where}.namespaces")
    if not namespaces:
        raise ConfigError(f"'{where}.namespaces' must name at least one namespace")

    return ServiceCaller(
        name=str(data["name"]).strip().lower(),
        subject=str(data["subject"]),
        namespaces=tuple(namespaces),
        assertion=AssertionSettings(
            jwks_url=jwks_url,
            audience=audience,
            issuer=str(data["issuer"]),
            required_claims=SERVICE_REQUIRED_CLAIMS,
        ),
        k8s_namespaces=_k8s_namespaces(data.get("k8s_namespaces"), where),
        rate_limit_per_minute=_rate_limit(data.get("rate_limit_per_minute"), where),
    )


def _k8s_namespaces(value: object, where: str) -> tuple[str, ...]:
    """Kubernetes namespaces this caller may place an object in, beyond the default.

    Absent or empty is the safe answer, not an oversight: a caller with none
    can still reach the application's own namespace, which is where onboarding
    puts it. Each entry is validated as a Kubernetes name here, so a typo is a
    config error rather than a document the API server rejects later.
    """
    field_path = f"{where}.k8s_namespaces"
    names = _names(value, field_path)
    for name in names:
        if not _K8S_NAME_RE.match(name) or len(name) > _K8S_NAME_MAX:
            raise ConfigError(
                f"'{field_path}' entry {name!r} is not a Kubernetes namespace name "
                "(lowercase letters, digits and hyphens, starting and ending alphanumeric)"
            )
    return tuple(names)


def _rate_limit(value: object, where: str) -> int:
    if value is None:
        return DEFAULT_SERVICE_RATE_LIMIT
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigError(f"'{where}.rate_limit_per_minute' must be a positive whole number")
    return value


def _reject_ambiguous_callers(callers: list[ServiceCaller]) -> None:
    """Refuse two callers a token could not be told apart by.

    A token names an issuer and an audience, and the lane picks the caller from
    that pair. Two callers sharing the pair would make the choice of whose
    subject and namespaces apply an accident of ordering.
    """
    seen_names: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    for caller in callers:
        if caller.name in seen_names:
            raise ConfigError(f"'service_callers' names {caller.name!r} twice")
        seen_names.add(caller.name)
        pair = (caller.issuer, caller.audience)
        if pair in seen_pairs:
            raise ConfigError(
                f"'service_callers' has two callers with the same issuer and "
                f"audience ({caller.name!r} repeats one); a token could match either"
            )
        seen_pairs.add(pair)


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
    identity = parse_identity(config.identity)
    return WebSettings(
        identity=identity,
        access=parse_access(config.access),
        environment=str(environment) if environment else None,
        openbao_address=address or None,
        kv_mount=deployment.get("kv_mount") or DEFAULT_KV_MOUNT,
        ui_dist=Path(ui_dist) if ui_dist else None,
        reveal_enabled=bool(reveal.get("enabled", False)),
        service_callers=parse_service_callers(
            config.service_callers,
            human_audience=identity.assertion.audience if identity.assertion else None,
        ),
    )


def load_web_settings(env: Mapping[str, str] | None = None) -> WebSettings:
    """Load settings for a running process.

    ``LADON_CONFIG`` points at the deployment config. ``LADON_CONFIG_YAML``
    carries the same document inline, for a deployment that delivers config as
    environment variables rather than as a mounted file: a ConfigMap consumed
    with ``envFrom`` turns each key into a variable, so a ``config.yaml`` key
    becomes a variable of that name and never becomes a file.

    The path wins when both are set, and the inline value is not consulted
    then: a deployment that names a file and does not mount it has a mistake
    worth failing on, not one worth papering over with a second source.

    Both are optional: with no config the backend still starts and serves
    ``/healthz`` and ``/api/me``, with nobody holding any namespace, which is
    the honest answer for an instance that has not been configured yet.
    """
    env = os.environ if env is None else env
    path = env.get(CONFIG_PATH_ENV)
    inline = env.get(CONFIG_INLINE_ENV)
    if path:
        config = load_deployment_config(path)
    elif inline:
        config = load_deployment_config_text(inline)
    else:
        config = DeploymentConfig()
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
