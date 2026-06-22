"""Build an ordered, value-free plan of OpenBao writes (D3b, pure layer).

A :class:`WriteOp` is one write against the OpenBao API: a logical kind, the API
path it targets, and the payload to send. A :class:`Plan` is the ordered list of
ops for a bring-up step. This module is pure: it turns the already-rendered
``ladon.config`` objects (policies, auth methods, namespaces) into a plan and can
render that plan for review. Executing the plan against a live OpenBao is the
separate SDK wrapper.

Ordering matters for OpenBao bring-up: a namespace must exist before anything is
mounted in it, a secrets engine and auth method must be enabled before their
config and roles are written, and policies should exist before roles reference
them. The builders here emit ops in that order so the plan can be applied top to
bottom.

Secrets hygiene: an op's :meth:`WriteOp.summary` shows only its kind and path,
never the payload, so a rendered plan never leaks a value or a secret reference.
The phase-4 root config this module plans does not carry secret *values* at all
(JWT uses a discovery URL; the OIDC client secret is a reference resolved at
execution), but the summary stays value-free regardless, so the same contract
holds when later phases plan secret writes.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from ladon.config.auth import AuthMethod, jwt_auth, jwt_role
from ladon.config.policies import DEFAULT_KV_MOUNT, Policy, external_secrets_read
from ladon.config.teams import ConfigError

# KV version 2 is the baseline (docs/baseline.md); the mount is enabled with the
# version pinned so an upgrade-in-place can never silently change the engine.
KV_VERSION = "2"

# Per-team ESO onboarding defaults, matching the source platform's convention
# (one JWT mount per cluster, a single open role bound by audience). These are
# parameters on the builder, not hardcoded behavior, so a deployment can override
# any of them; the defaults just keep the common case faithful to the source.
DEFAULT_ESO_POLICY = "external-secrets-readonly"
DEFAULT_ESO_ROLE = "external-secrets-root"
DEFAULT_ESO_AUDIENCE = "bao"


@dataclass(frozen=True)
class WriteOp:
    """One write against the OpenBao API.

    ``kind`` is a stable label for the operation (used in the rendered plan and
    by the executor to dispatch to the right SDK call). ``path`` is the API path
    relative to the mount root. ``data`` is the payload; it is never shown in a
    summary.
    """

    kind: str
    path: str
    data: dict = field(default_factory=dict)

    def summary(self) -> str:
        """A single value-free line describing the op: ``kind path``.

        Deliberately excludes ``data``: payloads can carry secret references and
        must never reach a log or a review surface.
        """
        return f"{self.kind} {self.path}"


@dataclass(frozen=True)
class Plan:
    """An ordered list of write ops for one bring-up step."""

    ops: tuple[WriteOp, ...] = ()

    def __len__(self) -> int:
        return len(self.ops)

    def render(self) -> str:
        """Render the plan as value-free summary lines, one op per line.

        Empty plans render a single explanatory line rather than nothing, so a
        dry-run never looks like it silently did work.
        """
        if not self.ops:
            return "(empty plan: no operations)"
        return "\n".join(op.summary() for op in self.ops)


def _enable_secrets_engine_op(mount: str, *, engine_type: str, version: str) -> WriteOp:
    return WriteOp(
        kind="enable-secrets-engine",
        path=f"sys/mounts/{mount}",
        data={"type": engine_type, "options": {"version": version}},
    )


def _enable_auth_method_op(method: AuthMethod) -> WriteOp:
    return WriteOp(
        kind="enable-auth-method",
        path=f"sys/auth/{method.path}",
        data={"type": method.type},
    )


def _write_policy_op(policy: Policy) -> WriteOp:
    return WriteOp(
        kind="write-policy",
        path=f"sys/policies/acl/{policy.name}",
        data={"policy": policy.hcl},
    )


def _configure_auth_op(method: AuthMethod) -> WriteOp:
    return WriteOp(
        kind="configure-auth-method",
        path=f"auth/{method.path}/config",
        data=dict(method.config),
    )


def _write_auth_role_ops(method: AuthMethod) -> list[WriteOp]:
    return [
        WriteOp(
            kind="write-auth-role",
            path=f"auth/{method.path}/role/{role.name}",
            data=dict(role.data),
        )
        for role in method.roles
    ]


def kv_mount_ops(mount: str = DEFAULT_KV_MOUNT) -> list[WriteOp]:
    """Enable the baseline KV v2 secrets engine at ``mount``."""
    return [_enable_secrets_engine_op(mount, engine_type="kv", version=KV_VERSION)]


def policy_ops(policies: Iterable[Policy]) -> list[WriteOp]:
    """Write each rendered policy. Policies come before roles that name them."""
    return [_write_policy_op(p) for p in policies]


def auth_method_ops(method: AuthMethod) -> list[WriteOp]:
    """Enable an auth method, configure its mount, then write its roles.

    The order is required by OpenBao: the mount must exist before its config and
    roles can be written.
    """
    return [
        _enable_auth_method_op(method),
        _configure_auth_op(method),
        *_write_auth_role_ops(method),
    ]


def root_config_plan(
    *,
    policies: Sequence[Policy],
    auth_methods: Sequence[AuthMethod],
    kv_mount: str = DEFAULT_KV_MOUNT,
) -> Plan:
    """Plan the phase-4 root config: KV mount, policies, then auth methods.

    This is the foundation the team-onboarding phase builds on. The order is:
    enable the KV engine, write the policies (so roles can reference them), then
    enable and configure each auth method with its roles. Duplicate policy names
    or auth mount paths are rejected: they would collide in OpenBao and almost
    always signal a config mistake.
    """
    policy_names = [p.name for p in policies]
    _reject_duplicates(policy_names, what="policy name")

    auth_paths = [m.path for m in auth_methods]
    _reject_duplicates(auth_paths, what="auth mount path")

    ops: list[WriteOp] = []
    ops += kv_mount_ops(kv_mount)
    ops += policy_ops(policies)
    for method in auth_methods:
        ops += auth_method_ops(method)
    return Plan(ops=tuple(ops))


def team_onboarding_plan(
    *,
    cluster: str,
    oidc_discovery_url: str,
    kv_mount: str = DEFAULT_KV_MOUNT,
    policy_name: str = DEFAULT_ESO_POLICY,
    role_name: str = DEFAULT_ESO_ROLE,
    audience: str = DEFAULT_ESO_AUDIENCE,
) -> Plan:
    """Plan the phase-10 per-team ESO onboarding inside an existing namespace.

    Faithful to the source platform: enable a JWT auth mount named
    ``jwt-<cluster>`` (one per cluster), configure it with the cluster's OIDC
    discovery URL (also used as the bound issuer) and a default role, write the
    ESO read-only policy, then write a JWT role bound by audience to that policy.

    The team's OpenBao namespace is assumed to already exist (created at root
    config / namespace request time); this plan operates within it and does not
    create it. The client applying the plan is expected to be scoped to that
    namespace. Ordering puts the policy before the role that references it, as
    OpenBao requires.
    """
    if not cluster:
        raise ConfigError("team onboarding requires a non-empty 'cluster'")

    mount = f"jwt-{cluster}"

    method = jwt_auth(
        discovery_url=oidc_discovery_url,
        path=mount,
        bound_issuer=oidc_discovery_url,
        default_role=role_name,
    )
    role = jwt_role(
        role_name,
        policies=[policy_name],
        bound_audiences=[audience],
    )
    # Reuse the baseline ESO read policy's HCL, named to the onboarding
    # convention so policies.py stays the single source of truth for the grant.
    base = external_secrets_read(kv_mount)
    policy = Policy(name=policy_name, hcl=base.hcl)

    ops: list[WriteOp] = [
        _enable_auth_method_op(method),
        _configure_auth_op(method),
        _write_policy_op(policy),
        WriteOp(
            kind="write-auth-role",
            path=f"auth/{mount}/role/{role.name}",
            data=dict(role.data),
        ),
    ]
    return Plan(ops=tuple(ops))


def _reject_duplicates(names: Sequence[str], *, what: str) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ConfigError(f"duplicate {what} {name!r} in plan")
        seen.add(name)
