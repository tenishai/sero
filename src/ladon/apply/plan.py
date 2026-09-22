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

An op also carries the OpenBao namespace it runs in. Namespaces are the
isolation the model rests on: the same mount name in two namespaces is two
different objects, so the namespace is part of what an op identifies, not a
property of the connection. ``None`` means "wherever the client already points",
which is the root for a normal apply.

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

from ladon.config.apps import AppIdentity, eso_app_policy, eso_app_role
from ladon.config.auth import AuthMethod, jwt_auth, jwt_role
from ladon.config.eso import DEFAULT_ESO_NAMESPACE, DEFAULT_ESO_SA
from ladon.config.policies import DEFAULT_KV_MOUNT, Policy, external_secrets_read
from ladon.config.teams import ConfigError, Team

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
    summary. ``namespace`` is the OpenBao namespace the op runs in, or None to
    run wherever the client already points.
    """

    kind: str
    path: str
    data: dict = field(default_factory=dict)
    namespace: str | None = None

    def summary(self) -> str:
        """A single value-free line describing the op: ``[namespace] kind path``.

        Deliberately excludes ``data``: payloads can carry secret references and
        must never reach a log or a review surface. The namespace is a name, and
        showing it is the only way a reviewer can tell two identical paths in
        two namespaces apart.
        """
        scope = f"[{self.namespace}] " if self.namespace else ""
        return f"{scope}{self.kind} {self.path}"

    def scoped_to(self, namespace: str | None) -> WriteOp:
        """A copy of this op running in ``namespace`` (unchanged if None)."""
        if not namespace:
            return self
        return WriteOp(kind=self.kind, path=self.path, data=self.data, namespace=namespace)


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


def ensure_namespace_op(name: str) -> WriteOp:
    """Ensure an OpenBao namespace exists, created from the namespace above it.

    ``sys/namespaces/<name>`` is itself namespace-scoped: creating ``team-a``
    from the root is ``sys/namespaces/team-a``, so the op is unscoped and it is
    the *contents* of the namespace that carry it. A namespace is only ever
    created, never deleted or renamed: everything a team holds lives inside it.

    ``name`` is one segment. A path would ask OpenBao to create a nested
    namespace from the root, which this op does not do: a nested namespace is
    created by running this same op inside its parent, which is the ``namespace``
    field's job. Silently posting the path would create a namespace literally
    named with a slash on some versions and fail on others.
    """
    if not name:
        raise ConfigError("ensure-namespace requires a non-empty name")
    if "/" in name:
        raise ConfigError(
            f"ensure-namespace name {name!r} must be a single segment; create a "
            "nested namespace by running this operation inside its parent"
        )
    return WriteOp(kind="ensure-namespace", path=f"sys/namespaces/{name}")


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


def team_namespace_ops(team: Team, kv_mount: str = DEFAULT_KV_MOUNT) -> list[WriteOp]:
    """Create one team's namespace and the baseline it starts with.

    Inside the namespace: the KV v2 mount the deployment names, and the policies
    the team's configured access level grants, plus the per-team ESO read
    policy, which every team needs to sync secrets and which grants no write. A
    team configured ``readonly`` therefore never gets a write policy written at
    all, rather than getting one nothing is supposed to bind to.
    """
    ops = [ensure_namespace_op(team.namespace)]
    inside = [
        *kv_mount_ops(kv_mount),
        *policy_ops([*team.policies(kv_mount), external_secrets_read(kv_mount)]),
    ]
    return ops + [op.scoped_to(team.namespace) for op in inside]


def root_config_plan(
    *,
    policies: Sequence[Policy],
    auth_methods: Sequence[AuthMethod],
    kv_mount: str = DEFAULT_KV_MOUNT,
    teams: Sequence[Team] = (),
    only_teams: Sequence[str] | None = None,
) -> Plan:
    """Plan the phase-4 root config: the platform root, then each team's namespace.

    This is the foundation the team-onboarding phase builds on. At the root the
    order is: enable the KV engine, write the policies (so roles can reference
    them), then enable and configure each auth method with its roles. Each team
    in the deployment config then gets its namespace and the baseline inside it.

    ``only_teams`` narrows the run to the named teams: their namespaces and the
    baseline inside them, and nothing at the root. That is what an approved
    namespace request applies, and it is why approving one team's request cannot
    rewrite the deployment's auth methods. A name the config does not define is
    refused: the namespace, the mount and the policies all come from that entry.

    Duplicate root policy names or auth mount paths are rejected: they would
    collide in OpenBao and almost always signal a config mistake. The teams are
    not part of that check. Each team's templates are rendered once inside its
    own namespace, where the same name is a different object, so every team
    carrying the same three names is the design and not a collision.
    """
    if only_teams is not None:
        selected = _select_teams(teams, only_teams)
        return Plan(ops=tuple(op for team in selected for op in team_namespace_ops(team, kv_mount)))

    _reject_duplicates([p.name for p in policies], what="policy name")
    _reject_duplicates([m.path for m in auth_methods], what="auth mount path")

    ops: list[WriteOp] = []
    ops += kv_mount_ops(kv_mount)
    ops += policy_ops(policies)
    for method in auth_methods:
        ops += auth_method_ops(method)
    for team in teams:
        ops += team_namespace_ops(team, kv_mount)
    return Plan(ops=tuple(ops))


def team_onboarding_plan(
    *,
    cluster: str,
    oidc_discovery_url: str,
    kv_mount: str = DEFAULT_KV_MOUNT,
    policy_name: str = DEFAULT_ESO_POLICY,
    role_name: str = DEFAULT_ESO_ROLE,
    audience: str = DEFAULT_ESO_AUDIENCE,
    eso_namespace: str = DEFAULT_ESO_NAMESPACE,
    eso_sa: str = DEFAULT_ESO_SA,
    namespace: str | None = None,
) -> Plan:
    """Plan the phase-10 per-team ESO onboarding inside an existing namespace.

    Faithful to the source platform: enable a JWT auth mount named
    ``jwt-<cluster>`` (one per cluster), configure it with the cluster's OIDC
    discovery URL (also used as the bound issuer) and a default role, write the
    ESO read-only policy, then write a JWT role bound to that policy.

    The role is bound by audience *and* by subject: any workload in the cluster
    can mint a token for a known audience, so the audience alone does not
    identify ESO. ``eso_namespace`` and ``eso_sa`` are where the operator runs
    and the service account it runs as, matching what the store renderer
    (``config.eso``) points at.

    Note what that subject pins and what it does not: it pins the role to the
    External Secrets Operator's own identity, not to the team, because one
    operator reads for every team on the cluster. The store's namespace selector
    is what keeps a team's secrets to a team's Kubernetes namespaces, until
    per-application identities replace the shared operator.

    The team's OpenBao namespace is assumed to already exist (created at root
    config / namespace request time); this plan operates within it and does not
    create it. ``namespace`` is that namespace: every op carries it, so one
    connection can onboard several teams and no op can land in the root by
    accident. Ordering puts the policy before the role that references it, as
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
        bound_subject=f"system:serviceaccount:{eso_namespace}:{eso_sa}",
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
    return Plan(ops=tuple(op.scoped_to(namespace) for op in ops))


def app_onboarding_plan(identity: AppIdentity) -> Plan:
    """Plan one application's own ESO identity inside its team's namespace (D17).

    Two ops: the policy scoped to the application's path prefix, then the JWT
    role that carries it, bound to the application's own service account. The
    policy comes first because the role names it.

    The cluster half of onboarding (the service account and the store) is not a
    plan: it is Kubernetes manifests, rendered by ``ladon.config.apps`` and
    applied by the cluster layer.

    The ``jwt-<cluster>`` mount this role hangs off is created once per cluster
    by team onboarding, so it is not repeated here.
    """
    policy = eso_app_policy(identity)
    role = eso_app_role(identity)
    ops = (
        _write_policy_op(policy),
        WriteOp(
            kind="write-auth-role",
            path=f"auth/{identity.auth_mount}/role/{role.name}",
            data=dict(role.data),
        ),
    )
    return Plan(ops=tuple(op.scoped_to(identity.namespace) for op in ops))


def _select_teams(teams: Sequence[Team], names: Sequence[str]) -> list[Team]:
    """The named teams, in the order they were named."""
    by_name = {team.name: team for team in teams}
    missing = [name for name in names if name not in by_name]
    if missing:
        raise ConfigError(
            f"no team named {', '.join(repr(n) for n in missing)} in the deployment config"
        )
    return [by_name[name] for name in names]


def _reject_duplicates(names: Sequence[str], *, what: str) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ConfigError(f"duplicate {what} {name!r} in plan")
        seen.add(name)
