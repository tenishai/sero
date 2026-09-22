"""Audit the JWT roles and the policies behind them (D17, the role audit).

Ladon renders scoped identities: a policy that grants read on one application's
prefix, and a JWT role that carries that policy and refuses the namespace's
``default``. None of that is worth anything if the objects drift afterwards, and
the drift that matters is invisible from the outside: a role still names its
scoped policy, but no longer refuses ``default``; ``default`` still exists, but
has been widened to full KV access plus ``sudo`` on the policy endpoints. Every
role then has every capability and every scoped policy is decorative. The
sibling platform ran that way for months, because nothing looked at it.

This module looks at it. It is read-only by construction: it reads mounts,
roles, and policy bodies, and returns :class:`Finding` objects. It never writes,
and it never returns a policy body or a field value. A finding names the object
and the path and capability at issue, which is what a reviewer needs and the
most that can be shown without turning an audit report into a disclosure.

What it checks, per namespace:

* every role on every JWT auth mount: that it refuses ``default``, does not name
  ``default`` among its policies, names at least one policy, names only policies
  that exist, binds an audience, and (for the ``eso-`` roles Ladon renders) pins
  a subject;
* the ``default`` policy body: that it reaches none of ``sys/policies/*``,
  ``sys/auth/*`` or ``sys/mounts/*``, grants nothing beyond read and list on a
  KV mount's data and metadata paths, and grants no ``sudo``-like capability
  anywhere;
* every ``eso-<app>-readonly`` policy: that it grants nothing outside that
  application's own prefix and the token-self paths, that no grant reaches
  another application's prefix, and that nothing beyond read and list is granted
  on the data it can reach.

The KV mounts are read from the namespace rather than assumed, so a deployment
that renamed its mount is audited against the mount it actually has.

A namespace is ``ok`` when it has no ``high`` findings. A ``medium`` is a
deviation an operator should look at (a role that grants nothing, a policy this
module cannot parse); a ``high`` is a grant that is wider than it was meant to
be.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from ladon.apply.executor import ApplyError, read_or_none, scoped_namespace, unwrap
from ladon.config.apps import APP_PATH_SEGMENT
from ladon.config.policies import DEFAULT_KV_MOUNT

# Severity, most serious first. The order is the render order, so the grant that
# widens access is the first line an operator reads.
HIGH = "high"
MEDIUM = "medium"
LOW = "low"
SEVERITIES = (HIGH, MEDIUM, LOW)

# The exit code the CLI uses for "the audit ran and found something serious",
# kept distinct from the code for "the audit could not run".
AUDIT_FINDINGS_EXIT = 3

# What the root namespace is called in a report. The root has no name in the
# API (it is the absence of a namespace header), and an empty label in a report
# line reads as a bug.
ROOT_LABEL = "root"

DEFAULT_POLICY = "default"

# Ladon names every per-application role and service account ``eso-<team>-<app>
# -<digest>`` (config/apps.py). The prefix is what marks a role as one whose
# subject must be pinned; an operator's own role is a different thing.
ESO_ROLE_PREFIX = "eso-"

# The per-application policy name, from which the application is read back.
_APP_POLICY_RE = re.compile(r"^eso-(?P<app>.+)-readonly$")

# Capabilities that do not widen anything: the two read verbs, plus "deny",
# which only ever narrows.
_NOT_A_WIDENING = frozenset({"read", "list", "deny"})

# Capabilities that lift the policy system's own limits. OpenBao spells this
# "sudo"; "root" is accepted here because a deployment that wrote it meant the
# same thing and it must not read as an ordinary verb.
_PRIVILEGED_CAPABILITIES = frozenset({"sudo", "root"})

# The system paths the default policy must never reach: granting any of them to
# every token is how a scoped-policy model stops meaning anything.
_SYSTEM_PREFIXES = ("sys/policies/", "sys/auth/", "sys/mounts/")

# The paths a token needs to look itself up, renew itself, revoke itself, and
# answer a capability check. A scoped policy carries them because the role
# refuses "default", which is what normally grants them, so they are expected
# rather than a widening.
_TOKEN_SELF_PATHS = frozenset(
    {
        "auth/token/lookup-self",
        "auth/token/renew-self",
        "auth/token/revoke-self",
        "sys/capabilities-self",
    }
)

# The two halves of a KV v2 mount a read-only identity touches.
_KV_SEGMENTS = ("data", "metadata")


class AuditError(RuntimeError):
    """The audit could not read what it needs in order to judge a namespace."""


class PolicyParseError(ValueError):
    """A policy body is not in the block form this parser accepts.

    The message names a position, never the content at it: a body this module
    could not parse is exactly the body it cannot vouch for, so it must not be
    echoed into a report.
    """


@dataclass(frozen=True)
class Finding:
    """One deviation, named without showing a policy body or a field value."""

    severity: str
    code: str
    object: str
    detail: str

    def render(self) -> str:
        return f"{self.severity} {self.code} {self.object}: {self.detail}"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AuditReport:
    """What one namespace's roles and policies look like right now."""

    namespace: str
    findings: tuple[Finding, ...] = ()

    @property
    def high(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == HIGH)

    @property
    def ok(self) -> bool:
        """True when nothing here grants more than it was meant to."""
        return not self.high

    def render(self) -> str:
        """One line per finding, prefixed with the namespace it was found in."""
        return "\n".join(f"[{self.namespace}] {f.render()}" for f in self.findings)

    def as_dict(self) -> dict:
        return {
            "namespace": self.namespace,
            "ok": self.ok,
            "findings": [f.as_dict() for f in self.findings],
        }


@dataclass(frozen=True)
class PathGrant:
    """One ``path`` block from a policy body: where, and what on it."""

    path: str
    capabilities: tuple[str, ...]

    def render(self) -> str:
        caps = ", ".join(self.capabilities) or "nothing"
        return f"{caps} on {self.path}"


# --- parsing policy bodies ------------------------------------------------------
#
# OpenBao returns a policy as HCL text, and Ladon needs to know what it grants.
# A full HCL parser is a dependency for one block form, so this is a strict
# reader of exactly the form Ladon renders (config/policies.py) and the sibling
# platform writes: a sequence of `path "<p>" { capabilities = [...] }` blocks,
# with comments and whitespace between them. Strictness is the point: a body
# with anything else in it is reported as unparsed, rather than being partly
# understood and silently vouched for.

_BLOCK = re.compile(
    r'path\s+"(?P<path>[^"\n]+)"\s*\{\s*capabilities\s*=\s*\[(?P<caps>[^\]]*)\]\s*,?\s*\}'
)
_CAPABILITY = re.compile(r'^"(?P<name>[A-Za-z0-9_-]+)"$')


def _strip_comments(text: str) -> str:
    """Remove ``#`` and ``//`` line comments that are not inside a quoted path."""
    out: list[str] = []
    in_string = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == '"':
            in_string = not in_string
        if not in_string and (char == "#" or text.startswith("//", index)):
            while index < len(text) and text[index] != "\n":
                index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _capabilities(raw: str, position: int) -> tuple[str, ...]:
    caps: list[str] = []
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        match = _CAPABILITY.match(item)
        if match is None:
            raise PolicyParseError(f"expected a quoted capability at character {position}")
        caps.append(match.group("name"))
    return tuple(caps)


def parse_policy(hcl: str) -> tuple[PathGrant, ...]:
    """The grants in a policy body, or :class:`PolicyParseError`.

    An empty body parses to no grants, which is a policy that grants nothing.
    """
    text = _strip_comments(str(hcl))
    grants: list[PathGrant] = []
    cursor = 0
    while cursor < len(text):
        if text[cursor].isspace():
            cursor += 1
            continue
        match = _BLOCK.match(text, cursor)
        if match is None:
            raise PolicyParseError(f"expected a path block at character {cursor}")
        grants.append(
            PathGrant(
                path=match.group("path"),
                capabilities=_capabilities(match.group("caps"), cursor),
            )
        )
        cursor = match.end()
    return tuple(grants)


# --- reasoning about paths --------------------------------------------------------


def _covers(granted: str, target: str) -> bool:
    """True when ``granted`` can reach anything at or under ``target``.

    OpenBao globs only at the end of a path, so a granted path is either a
    literal or a prefix with a trailing ``*``. A glob covers the target when
    either one is a prefix of the other: ``sys/*`` reaches into
    ``sys/policies/``, and ``sys/policies/acl/*`` is already inside it.
    """
    if granted.endswith("*"):
        prefix = granted[:-1]
        return target.startswith(prefix) or prefix.startswith(target)
    return granted == target.rstrip("/") or granted.startswith(target)


def _widening(grant: PathGrant) -> tuple[str, ...]:
    """The capabilities in a grant that are more than read and list."""
    return tuple(sorted(set(grant.capabilities) - _NOT_A_WIDENING))


def _privileged(grant: PathGrant) -> tuple[str, ...]:
    return tuple(sorted(set(grant.capabilities) & _PRIVILEGED_CAPABILITIES))


def _kv_prefixes(kv_mounts: Sequence[str]) -> tuple[str, ...]:
    return tuple(f"{mount}/{segment}/" for mount in kv_mounts for segment in _KV_SEGMENTS)


def _app_prefixes(kv_mounts: Sequence[str]) -> tuple[str, ...]:
    return tuple(f"{prefix}{APP_PATH_SEGMENT}/" for prefix in _kv_prefixes(kv_mounts))


def _own_prefixes(app: str, kv_mounts: Sequence[str]) -> tuple[str, ...]:
    return tuple(f"{prefix}{app}" for prefix in _app_prefixes(kv_mounts))


def _within_own_prefix(path: str, app: str, kv_mounts: Sequence[str]) -> bool:
    """True when the grant stays inside the application's own prefix.

    The three accepted shapes are the ones Ladon renders and the one the
    baseline documents: the bare path, everything under it, and the prefix glob
    over both. A path that merely starts with the application's name (a
    neighbour whose name extends it) is deliberately not one of them.
    """
    for own in _own_prefixes(app, kv_mounts):
        if path in (own, f"{own}*") or path.startswith(f"{own}/"):
            return True
    return False


def _reaches_another_app(path: str, app: str, kv_mounts: Sequence[str]) -> bool:
    """True when the grant can read some other application's prefix."""
    if _within_own_prefix(path, app, kv_mounts):
        return False
    return any(_covers(path, base) for base in _app_prefixes(kv_mounts))


# --- reading live state -------------------------------------------------------------


def _kv_mounts(client) -> tuple[str, ...]:
    """The KV mounts in the namespace, or the baseline default if it has none.

    Read rather than assumed: a deployment names its own mount, and auditing a
    renamed mount against the default would quietly check nothing.
    """
    mounts = unwrap(read_or_none(client.sys.list_mounted_secrets_engines))
    found = tuple(
        sorted(
            name.rstrip("/")
            for name, mount in mounts.items()
            if isinstance(mount, dict) and mount.get("type") == "kv"
        )
    )
    return found or (DEFAULT_KV_MOUNT,)


def _jwt_mounts(client) -> tuple[str, ...]:
    methods = unwrap(read_or_none(client.sys.list_auth_methods))
    return tuple(
        sorted(
            name.rstrip("/")
            for name, method in methods.items()
            if isinstance(method, dict) and method.get("type") == "jwt"
        )
    )


def _role_names(client, mount: str) -> tuple[str, ...]:
    listing = unwrap(read_or_none(client.list, f"auth/{mount}/role"))
    return tuple(sorted(str(key) for key in listing.get("keys") or ()))


def _policy_names(client) -> frozenset[str]:
    listing = unwrap(read_or_none(client.sys.list_policies))
    # The ACL listing answers under "policies"; the generic list endpoint under
    # "keys". Either way it is the same set of names.
    names = listing.get("policies") or listing.get("keys") or ()
    return frozenset(str(name) for name in names)


def _policy_body(client, name: str) -> str | None:
    response = read_or_none(client.sys.read_policy, name=name)
    if response is None:
        return None
    live = unwrap(response)
    # The read endpoint returns the body under "rules"; newer responses use
    # "policy". Either way it is the same HCL.
    body = live.get("rules")
    if body is None:
        body = live.get("policy")
    return None if body is None else str(body)


def _grants(client, name: str) -> tuple[tuple[PathGrant, ...], list[Finding]]:
    """A policy's grants, or the one finding that says it could not be read."""
    body = _policy_body(client, name)
    if body is None:
        return (), []
    try:
        return parse_policy(body), []
    except PolicyParseError as exc:
        return (), [
            Finding(
                MEDIUM,
                "unparsed-policy",
                name,
                f"could not be read as path blocks, so its grants were not checked: {exc}",
            )
        ]


# --- the checks -----------------------------------------------------------------------


def _is_true(value) -> bool:
    """OpenBao echoes a boolean back as a bool or as the string "true"."""
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() == "true"


def _as_names(value) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    return ()


def _audit_role(mount: str, name: str, data: dict, *, known: frozenset[str]) -> list[Finding]:
    obj = f"{mount}/role/{name}"
    findings: list[Finding] = []

    if not _is_true(data.get("token_no_default_policy")):
        findings.append(
            Finding(
                HIGH,
                "role-inherits-default",
                obj,
                "token_no_default_policy is not set, so this role also carries the "
                "namespace default policy",
            )
        )

    policies = _as_names(data.get("token_policies"))
    if not policies:
        findings.append(
            Finding(
                MEDIUM,
                "no-token-policies",
                obj,
                "token_policies is empty, so this role grants nothing of its own",
            )
        )
    elif DEFAULT_POLICY in policies:
        findings.append(
            Finding(
                HIGH,
                "default-in-token-policies",
                obj,
                f"token_policies names the {DEFAULT_POLICY} policy, whose grants are "
                "outside Ladon's control",
            )
        )
    for policy in policies:
        if policy != DEFAULT_POLICY and policy not in known:
            findings.append(
                Finding(
                    MEDIUM,
                    "missing-policy",
                    obj,
                    f"token_policies names {policy}, which does not exist in this namespace",
                )
            )

    if not _as_names(data.get("bound_audiences")):
        findings.append(
            Finding(
                MEDIUM,
                "unbound-audience",
                obj,
                "bound_audiences is empty, so a token minted for any audience is accepted",
            )
        )

    if name.startswith(ESO_ROLE_PREFIX) and not data.get("bound_subject"):
        findings.append(
            Finding(
                HIGH,
                "unbound-subject",
                obj,
                "bound_subject is empty, so any workload that can mint a token for the "
                "bound audience can use this role",
            )
        )
    return findings


def _audit_default_policy(client, *, kv_mounts: Sequence[str]) -> list[Finding]:
    grants, findings = _grants(client, DEFAULT_POLICY)
    kv_prefixes = _kv_prefixes(kv_mounts)
    for grant in grants:
        privileged = _privileged(grant)
        if privileged:
            findings.append(
                Finding(
                    HIGH,
                    "default-grants-sudo",
                    DEFAULT_POLICY,
                    f"grants {', '.join(privileged)} on {grant.path}",
                )
            )
        if any(_covers(grant.path, prefix) for prefix in _SYSTEM_PREFIXES):
            findings.append(
                Finding(
                    HIGH,
                    "default-grants-system",
                    DEFAULT_POLICY,
                    f"grants {grant.render()}",
                )
            )
        widening = _widening(grant)
        if widening and any(_covers(grant.path, prefix) for prefix in kv_prefixes):
            findings.append(
                Finding(
                    HIGH,
                    "default-widens-kv",
                    DEFAULT_POLICY,
                    f"grants {', '.join(widening)} on {grant.path}, beyond read and list",
                )
            )
    return findings


def _audit_app_policy(client, name: str, app: str, *, kv_mounts: Sequence[str]) -> list[Finding]:
    grants, findings = _grants(client, name)
    for grant in grants:
        if grant.path in _TOKEN_SELF_PATHS:
            continue
        if _reaches_another_app(grant.path, app, kv_mounts):
            findings.append(
                Finding(
                    HIGH,
                    "cross-app-grant",
                    name,
                    f"grants {grant.render()}, which reaches another application's prefix",
                )
            )
            continue
        if not _within_own_prefix(grant.path, app, kv_mounts):
            findings.append(
                Finding(
                    HIGH,
                    "out-of-scope-grant",
                    name,
                    f"grants {grant.render()}, outside the application's own prefix",
                )
            )
            continue
        widening = _widening(grant)
        if widening:
            findings.append(
                Finding(
                    HIGH,
                    "overbroad-capability",
                    name,
                    f"grants {', '.join(widening)} on {grant.path}, beyond read and list",
                )
            )
    return findings


def _audit_within(client) -> list[Finding]:
    """Every check, against whichever namespace the client is scoped to."""
    kv_mounts = _kv_mounts(client)
    known = _policy_names(client)

    findings: list[Finding] = []
    for mount in _jwt_mounts(client):
        for role in _role_names(client, mount):
            data = unwrap(read_or_none(client.read, f"auth/{mount}/role/{role}"))
            findings += _audit_role(mount, role, data, known=known)

    findings += _audit_default_policy(client, kv_mounts=kv_mounts)
    for name in sorted(known):
        match = _APP_POLICY_RE.match(name)
        if match:
            findings += _audit_app_policy(client, name, match.group("app"), kv_mounts=kv_mounts)
    return findings


def audit_namespace(client, namespace: str | None = None) -> AuditReport:
    """Audit one OpenBao namespace, reading only. ``None`` is the root.

    The whole namespace is read under one scope, so the report can never mix
    one namespace's roles with another's policies.
    """
    label = (namespace or "").strip("/") or ROOT_LABEL
    what = f"the audit of namespace {label}"
    try:
        with scoped_namespace(client, namespace, what=what):
            findings = _audit_within(client)
    except ApplyError as exc:
        raise AuditError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - re-raised with the namespace named
        raise AuditError(f"{what} failed: {exc}") from exc
    # A stable sort by severity keeps the order each check found things in,
    # with the grants that widen access at the top.
    ordered = sorted(findings, key=lambda finding: SEVERITIES.index(finding.severity))
    return AuditReport(namespace=label, findings=tuple(ordered))


def audit_all(client, namespaces: Sequence[str] = ()) -> tuple[AuditReport, ...]:
    """Audit the root and then each named namespace, in order.

    The root is always included: the platform's own auth mounts and its
    ``default`` policy live there, and a widened root default is inherited by
    every token the root issues.
    """
    return (
        audit_namespace(client),
        *(audit_namespace(client, namespace) for namespace in namespaces),
    )
