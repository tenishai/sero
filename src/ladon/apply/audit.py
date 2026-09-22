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

* every workload role on every JWT and OIDC auth mount. A role counts as a
  workload role when its ``role_type`` reads as ``jwt``, wherever it was
  written: an OIDC mount is not a place a JWT role is exempt. A role whose
  ``role_type`` is ``oidc`` keeps the documented exemption, because an
  operator's interactive session legitimately carries ``default`` (that is what
  lets a token renew and revoke itself) and has no service account to pin.
  Each workload role must refuse ``default``, name neither ``default`` nor
  ``root`` among its policies, name at least one policy that exists, bind an
  audience, and pin a ``bound_subject`` to one real service account. An
  ``eso-`` role must carry exactly its own ``eso-<app>-readonly`` policy and
  nothing else, bound to the service account of the same name.
* the ``default`` policy body: that it reaches none of ``sys/policies/*``,
  ``sys/auth/*`` or ``sys/mounts/*``, grants nothing beyond read and list on any
  segment of a KV mount (data, metadata, and the delete, undelete and destroy
  lifecycle paths beside them), never globs a whole KV mount even at read, and
  grants no ``sudo``-like capability anywhere.
* every ``eso-<app>-readonly`` policy: that it grants nothing outside that
  application's own prefix and the token-self paths, that no grant reaches
  another application's prefix, and that nothing beyond read and list is granted
  on the data it can reach. The token-self paths are an exemption from the
  prefix rule, not from the capability rule.

An explicit ``deny`` is never a finding: it narrows, and an audit that punishes
hardening teaches the wrong lesson.

The KV mounts are read from the namespace rather than assumed, and no default
is invented when there are none: a mount guessed into existence produces an
audit that reports clean about paths it never looked at.

A namespace is ``ok`` when it has no ``high`` findings. A ``high`` is a grant
wider than it was meant to be, or a policy this audit claims to judge and could
not read. A ``medium`` is a deviation an operator should look at (a role that
grants nothing, a namespace with nothing in it to judge). A ``low`` is context
a reader needs to weigh the rest. Every report carries the counts of what it
looked at, because "clean" about an empty namespace is the failure mode this
whole module exists to prevent.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

from ladon.apply.executor import ApplyError, read_or_none, scoped_namespace, unwrap
from ladon.config.apps import APP_PATH_SEGMENT

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

# Policies whose grants no deployment controls and no workload role should ever
# name. OpenBao's "root" is unlimited by definition, and it is a real policy in
# the listing, so without this it reads as an ordinary name that exists.
PRIVILEGED_POLICIES = frozenset({"root"})

# Ladon names every per-application role and service account ``eso-<team>-<app>
# -<digest>`` (config/apps.py). The prefix is what marks a role as one that must
# carry exactly its own application policy and be bound to the service account
# of the same name.
ESO_ROLE_PREFIX = "eso-"

# The per-application policy name, from which the application is read back. The
# application part is greedy so a hyphenated name ("sample-other") is read whole.
_APP_POLICY_RE = re.compile(r"^eso-(?P<app>.+)-readonly$")

# Auth mounts whose roles this audit reads. Both kinds are read, because where a
# role is written says nothing about what it is.
_AUDITED_AUTH_TYPES = frozenset({"jwt", "oidc"})

# The role_type that marks a workload role. Anything else on an audited mount is
# a human role and keeps the exemption documented in baseline.md.
_WORKLOAD_ROLE_TYPE = "jwt"

# A Kubernetes service account subject, which is the only shape a workload
# subject takes here.
_SUBJECT_RE = re.compile(r"^system:serviceaccount:(?P<namespace>[^:]+):(?P<account>[^:]+)$")

# Service accounts nothing may be pinned to: "default" exists in every namespace
# and every pod there can use it, and the control-plane namespaces are shared
# with workloads no deployment owns.
_SHARED_ACCOUNT = "default"
_SYSTEM_NAMESPACE_PREFIX = "kube-"

# Capabilities that do not widen anything: the two read verbs, plus "deny",
# which only ever narrows.
_NOT_A_WIDENING = frozenset({"read", "list", "deny"})

# A grant that only denies. It is hardening, so no check looks at it: an audit
# that reports a deny block teaches operators that narrowing makes things worse.
_DENY_ONLY = frozenset({"deny"})

# Capabilities that lift the policy system's own limits. OpenBao spells this
# "sudo"; "root" is accepted here because a deployment that wrote it meant the
# same thing and it must not read as an ordinary verb.
_PRIVILEGED_CAPABILITIES = frozenset({"sudo", "root"})

# The system paths the default policy must never reach: granting any of them to
# every token is how a scoped-policy model stops meaning anything.
_SYSTEM_PREFIXES = ("sys/policies/", "sys/auth/", "sys/mounts/")

# The paths a token needs to look itself up, renew itself, revoke itself, and
# answer a capability check, with exactly the capability each one needs. A
# scoped policy carries them because the role refuses "default", which is what
# normally grants them, so they are expected outside the application's prefix.
# They are not expected to carry anything more than this: the exemption is from
# the prefix rule, not from the capability rule.
_TOKEN_SELF_CAPABILITIES = {
    "auth/token/lookup-self": frozenset({"read"}),
    "auth/token/renew-self": frozenset({"update"}),
    "auth/token/revoke-self": frozenset({"update"}),
    "sys/capabilities-self": frozenset({"update"}),
}

# Where an application's secrets live inside a KV v2 mount.
_KV_DATA_SEGMENTS = ("data", "metadata")

# Every segment of a KV v2 mount. The lifecycle paths sit beside data and
# metadata rather than under them, so a check that walks only the first two
# misses "destroy", which removes a secret version outright.
_KV_SEGMENTS = (*_KV_DATA_SEGMENTS, "delete", "undelete", "destroy")


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
    """What one namespace's roles and policies look like right now.

    ``counts`` is what the audit actually looked at: auth mounts, roles on
    them, and policies. It travels with every report because a clean report
    over nothing is indistinguishable from a clean report over a whole
    deployment, and the first one is worthless.
    """

    namespace: str
    findings: tuple[Finding, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)

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
            "counts": dict(self.counts),
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


def _denies_only(grant: PathGrant) -> bool:
    """True when the grant only denies, which no check has anything to say about."""
    return set(grant.capabilities) <= _DENY_ONLY


def _kv_prefixes(kv_mounts: Sequence[str]) -> tuple[str, ...]:
    return tuple(f"{mount}/{segment}/" for mount in kv_mounts for segment in _KV_SEGMENTS)


def _app_prefixes(kv_mounts: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        f"{mount}/{segment}/{APP_PATH_SEGMENT}/"
        for mount in kv_mounts
        for segment in _KV_DATA_SEGMENTS
    )


def _own_prefixes(app: str, kv_mounts: Sequence[str]) -> tuple[str, ...]:
    return tuple(f"{prefix}{app}" for prefix in _app_prefixes(kv_mounts))


def _within_own_prefix(path: str, app: str, kv_mounts: Sequence[str]) -> bool:
    """True when the grant stays inside the application's own prefix.

    Two shapes only: the bare path, and anything under it (``<own>/*``
    included). A prefix glob on the name itself (``apps/sample*``) is not one of
    them, and that is the point: it reads every neighbour whose name extends
    this one, which is a real arrangement rather than a hypothetical, because
    applications named ``sample`` and ``sample-other`` live in one namespace.
    """
    return any(path == own or path.startswith(f"{own}/") for own in _own_prefixes(app, kv_mounts))


def _globs_a_whole_mount(path: str, kv_mounts: Sequence[str]) -> bool:
    """True when the grant is a glob at or above a KV mount's segment root.

    ``kv/*``, ``kv/data/*`` and ``kv/metadata/*`` each reach every secret in
    the mount. A glob further down (``kv/data/shared/*``) does not, and a
    literal path never does.
    """
    if not path.endswith("*"):
        return False
    prefix = path[:-1]
    return any(root.startswith(prefix) for root in _kv_prefixes(kv_mounts))


def _reaches_another_app(path: str, app: str, kv_mounts: Sequence[str]) -> bool:
    """True when the grant can read some other application's prefix."""
    if _within_own_prefix(path, app, kv_mounts):
        return False
    return any(_covers(path, base) for base in _app_prefixes(kv_mounts))


# --- reading live state -------------------------------------------------------------


def _kv_mounts(client) -> tuple[str, ...]:
    """The KV mounts in the namespace. Empty when it has none.

    Read rather than assumed, and never defaulted: a deployment names its own
    mount, and a mount guessed into existence produces an audit that walks
    paths nothing uses and then reports clean about the ones it never saw.
    """
    mounts = unwrap(read_or_none(client.sys.list_mounted_secrets_engines))
    return tuple(
        sorted(
            name.rstrip("/")
            for name, mount in mounts.items()
            if isinstance(mount, dict) and mount.get("type") == "kv"
        )
    )


def _auth_mounts(client) -> tuple[tuple[str, str], ...]:
    """The JWT and OIDC auth mounts, each with its type.

    Both kinds are read. Where a role was written says nothing about what it
    is, and a JWT role on the OIDC mount is exactly the shape a check that
    looked only at JWT mounts would never see.
    """
    methods = unwrap(read_or_none(client.sys.list_auth_methods))
    return tuple(
        sorted(
            (name.rstrip("/"), str(method.get("type")))
            for name, method in methods.items()
            if isinstance(method, dict) and method.get("type") in _AUDITED_AUTH_TYPES
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


def _unparsed_severity(name: str) -> str:
    """How serious it is that this policy could not be read.

    High for the two the audit claims to judge: an unreadable ``default`` or
    application policy is exactly the one the report cannot vouch for, and
    calling the namespace clean would be a claim with nothing behind it. Medium
    for anything else, which is a deployment's own to shape and was never part
    of the claim.
    """
    if name == DEFAULT_POLICY or _APP_POLICY_RE.match(name):
        return HIGH
    return MEDIUM


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
                _unparsed_severity(name),
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


def _role_type(data: dict, mount_type: str) -> str:
    """What kind of role this is: what it declares, else what its mount implies.

    OpenBao fills ``role_type`` in from the mount when a role does not declare
    one, so an OIDC role written without it is an OIDC role. A role that does
    declare ``jwt`` is a workload role wherever it lives.
    """
    declared = data.get("role_type")
    if isinstance(declared, str) and declared.strip():
        return declared.strip().lower()
    return mount_type


def _policy_findings(obj: str, name: str, policies: Sequence[str], known: frozenset[str]):
    """What the set of policies a role carries says about it."""
    findings: list[Finding] = []
    if not policies:
        return [
            Finding(
                MEDIUM,
                "no-token-policies",
                obj,
                "token_policies is empty, so this role grants nothing of its own",
            )
        ]

    if DEFAULT_POLICY in policies:
        findings.append(
            Finding(
                HIGH,
                "default-in-token-policies",
                obj,
                f"token_policies names the {DEFAULT_POLICY} policy, whose grants are "
                "outside Ladon's control",
            )
        )
    for policy in sorted(set(policies) & PRIVILEGED_POLICIES):
        findings.append(
            Finding(
                HIGH,
                "privileged-policy",
                obj,
                f"token_policies names {policy}, which is unlimited by definition",
            )
        )

    # The two above are reported by name already, and neither is an ACL policy
    # a namespace lists, so neither is missing and neither is an "extra".
    named = [p for p in policies if p != DEFAULT_POLICY and p not in PRIVILEGED_POLICIES]
    for policy in named:
        if policy not in known:
            findings.append(
                Finding(
                    MEDIUM,
                    "missing-policy",
                    obj,
                    f"token_policies names {policy}, which does not exist in this namespace",
                )
            )

    if not name.startswith(ESO_ROLE_PREFIX):
        return findings

    # A per-application role exists to carry one application's policy. A second
    # policy beside it hands that application whatever the second one grants,
    # which is the whole boundary gone.
    own = [p for p in named if _APP_POLICY_RE.match(p)]
    for policy in named:
        if not _APP_POLICY_RE.match(policy):
            findings.append(
                Finding(
                    HIGH,
                    "extra-policy",
                    obj,
                    f"token_policies names {policy}, which is not a per-application "
                    "policy; an application role carries only its own eso-<app>-readonly",
                )
            )
    if len(own) > 1:
        findings.append(
            Finding(
                HIGH,
                "extra-policy",
                obj,
                f"token_policies names {len(own)} application policies; an application "
                "role carries exactly one, its own",
            )
        )
    return findings


def _subject_findings(obj: str, name: str, subject: str) -> list[Finding]:
    """What the bound subject says about who can actually use this role.

    The audience alone proves nothing: any workload on the cluster can mint a
    token for a known audience. The subject is the whole binding, so a subject
    that names more than one workload is a role that authenticates more than one
    workload.
    """
    if "*" in subject:
        return [
            Finding(
                HIGH,
                "subject-not-pinned",
                obj,
                "bound_subject carries a wildcard, so it matches more than one service account",
            )
        ]

    match = _SUBJECT_RE.match(subject)
    if match is None:
        # Not a Kubernetes service account subject. For an application role that
        # is already wrong; for anything else the shape is the deployment's own.
        if name.startswith(ESO_ROLE_PREFIX):
            return [
                Finding(
                    HIGH,
                    "subject-not-pinned",
                    obj,
                    "bound_subject is not a service account subject, so it cannot be "
                    "this role's own service account",
                )
            ]
        return []

    namespace, account = match.group("namespace"), match.group("account")
    if (
        account == _SHARED_ACCOUNT
        or namespace == _SHARED_ACCOUNT
        or namespace.startswith(_SYSTEM_NAMESPACE_PREFIX)
    ):
        return [
            Finding(
                HIGH,
                "subject-not-pinned",
                obj,
                f"bound_subject names {account} in namespace {namespace}, a default or "
                "system service account that workloads nobody onboarded can use",
            )
        ]

    if name.startswith(ESO_ROLE_PREFIX) and account != name:
        return [
            Finding(
                HIGH,
                "subject-not-pinned",
                obj,
                f"bound_subject names {account} in namespace {namespace}, not this "
                f"role's own service account {name}",
            )
        ]
    return []


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

    findings += _policy_findings(obj, name, _as_names(data.get("token_policies")), known)

    if not _as_names(data.get("bound_audiences")):
        findings.append(
            Finding(
                MEDIUM,
                "unbound-audience",
                obj,
                "bound_audiences is empty, so a token minted for any audience is accepted",
            )
        )

    # Every workload role, not only the application ones: a shared operator role
    # with no subject is one identity that reads the whole namespace, and it is
    # exactly the role a narrower check would wave through.
    subject = data.get("bound_subject")
    if not subject:
        findings.append(
            Finding(
                HIGH,
                "unbound-subject",
                obj,
                "bound_subject is empty, so any workload that can mint a token for the "
                "bound audience can use this role",
            )
        )
    else:
        findings += _subject_findings(obj, name, str(subject))
    return findings


def _audit_default_policy(client, *, kv_mounts: Sequence[str]) -> list[Finding]:
    grants, findings = _grants(client, DEFAULT_POLICY)
    kv_prefixes = _kv_prefixes(kv_mounts)
    for grant in grants:
        # A deny block narrows what every token can do. Nothing below has
        # anything to say about it.
        if _denies_only(grant):
            continue

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
        elif not widening and _globs_a_whole_mount(grant.path, kv_mounts):
            # Read on every path in the mount is every secret in the namespace,
            # handed to every token the namespace issues. The capability check
            # cannot see it, because read is not a widening of capability.
            findings.append(
                Finding(
                    HIGH,
                    "default-reads-all-kv",
                    DEFAULT_POLICY,
                    f"grants {grant.render()}, which is every secret in the mount",
                )
            )
    return findings


def _audit_app_policy(client, name: str, app: str, *, kv_mounts: Sequence[str]) -> list[Finding]:
    grants, findings = _grants(client, name)
    for grant in grants:
        if _denies_only(grant):
            continue

        allowed = _TOKEN_SELF_CAPABILITIES.get(grant.path)
        if allowed is not None:
            # Exempt from the prefix rule, not from the capability rule.
            extra = tuple(sorted(set(grant.capabilities) - allowed))
            if extra:
                findings.append(
                    Finding(
                        HIGH,
                        "overbroad-capability",
                        name,
                        f"grants {', '.join(extra)} on {grant.path}, beyond what a token "
                        "needs to manage itself",
                    )
                )
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


def _audit_within(client) -> tuple[list[Finding], dict[str, int]]:
    """Every check, against whichever namespace the client is scoped to.

    Returns the findings and what was looked at, so a caller can tell a clean
    namespace from an empty one.
    """
    kv_mounts = _kv_mounts(client)
    known = _policy_names(client)
    mounts = _auth_mounts(client)

    findings: list[Finding] = []
    if not kv_mounts:
        findings.append(
            Finding(
                LOW,
                "no-kv-mount",
                "secrets engines",
                "no KV mount in this namespace, so no grant here was measured against "
                "one; a mount assumed instead would audit paths nothing uses",
            )
        )

    roles = 0
    for mount, mount_type in mounts:
        for role in _role_names(client, mount):
            roles += 1
            data = unwrap(read_or_none(client.read, f"auth/{mount}/role/{role}"))
            if _role_type(data, mount_type) != _WORKLOAD_ROLE_TYPE:
                # A human role: it carries the default policy on purpose and has
                # no service account to pin (baseline.md).
                continue
            findings += _audit_role(mount, role, data, known=known)

    findings += _audit_default_policy(client, kv_mounts=kv_mounts)
    for name in sorted(known):
        match = _APP_POLICY_RE.match(name)
        if match:
            findings += _audit_app_policy(client, name, match.group("app"), kv_mounts=kv_mounts)

    counts = {"mounts": len(mounts), "roles": roles, "policies": len(known)}
    return findings, counts


def _empty_namespace_finding(label: str, counts: dict[str, int]) -> Finding | None:
    """Say so when there was nothing to judge, rather than reporting clean."""
    missing = [
        what for what, key in (("auth mount", "mounts"), ("policy", "policies")) if not counts[key]
    ]
    if not missing:
        return None
    return Finding(
        MEDIUM,
        "empty-namespace",
        label,
        "found no " + " and no ".join(missing) + " here, so this report judged nothing",
    )


def audit_namespace(client, namespace: str | None = None) -> AuditReport:
    """Audit one OpenBao namespace, reading only. ``None`` is the root.

    The whole namespace is read under one scope, so the report can never mix
    one namespace's roles with another's policies.
    """
    label = (namespace or "").strip("/") or ROOT_LABEL
    what = f"the audit of namespace {label}"
    try:
        with scoped_namespace(client, namespace, what=what):
            findings, counts = _audit_within(client)
    except ApplyError as exc:
        raise AuditError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - re-raised with the namespace named
        raise AuditError(f"{what} failed: {exc}") from exc

    empty = _empty_namespace_finding(label, counts)
    if empty is not None:
        findings.append(empty)
    # A stable sort by severity keeps the order each check found things in,
    # with the grants that widen access at the top.
    ordered = sorted(findings, key=lambda finding: SEVERITIES.index(finding.severity))
    return AuditReport(namespace=label, findings=tuple(ordered), counts=counts)


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
