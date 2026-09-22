"""Apply rendered documents to the cluster Ladon runs on (D18, D18a).

Ladon manages one cluster: its own. Documents rendered by the config layer
(``ladon.config.eso`` and the app-onboarding phase) reach that cluster through
``kubectl apply --server-side --field-manager=ladon``. Server-side apply gives
Ladon a named field manager, so every field it owns is recorded on the object
and another manager editing the same field is reported as a conflict rather than
silently overwritten. Ladon never passes ``--force-conflicts``: taking a field
away from whoever else manages it is a decision for an operator, not a default.

The same read-diff-write-verify rule the OpenBao apply layer follows applies
here. ``apply_documents`` writes, ``read_back`` reads the live objects, and
``verify`` compares only the fields the document sets, so server-set defaults
and controller status never read as drift. ``converge_documents`` is the four of
them in one call, and it is what gives the cluster half the same no-op signal
the OpenBao half has: a document that already matches is reported
``already current`` and never reaches kubectl at all.

``delete_documents`` is the other direction, and it is deliberately narrower
than an apply: an object is removed only when the live object records Ladon as
one of its field managers. Server-side apply is what makes that knowable, and
the rule is the mirror of never passing ``--force-conflicts``. Taking a field
from another manager needs an operator; so does deleting that manager's object.

Two probes support the ESO work (D18a): ``eso_api_version`` reports which CRD
version the cluster stores, and ``detect_eso`` finds an operator somebody else
already installed so Ladon can reuse it instead of fighting it for the CRDs.

Secrets hygiene: these documents carry references (a service account, a JWT
role, a KV path), never values. Error messages name the kind, the object and the
field path, and carry at most the first line of kubectl's stderr, truncated, so
a message that happens to echo an object body cannot reach a log.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from ladon.hygiene import WriteRefused

DEFAULT_FIELD_MANAGER = "ladon"

# What a document needs, in the vocabulary the OpenBao apply layer already uses.
# kubectl's own verbs cannot answer this: a server-side apply prints one verb
# whether it created or updated, and it has no word at all for a document the
# cluster already holds unchanged.
CREATE = "create"
UPDATE = "update"
ALREADY_CURRENT = "already current"
BLOCKED = "blocked"

# The two a removal reports. ``ABSENT`` is the no-op signal a delete needs, the
# way ``ALREADY_CURRENT`` is an apply's: asking twice is not an error.
DELETE = "delete"
ABSENT = "absent"

# The API server's reason code for an object that is not there. Only this counts
# as absent: any other refusal (a denial, an unknown resource type) means Ladon
# could not look, which is never the same as nothing being there.
NOT_FOUND_REASON = "(NotFound)"

# Every kubectl call here is a read or a single apply against one cluster; none
# of them is a long-running watch, so a call still open after this has hung.
DEFAULT_TIMEOUT_SECONDS = 120

# At most this much of one error line reaches a message. kubectl can echo an
# object body into an error, and a truncated field path is still diagnosable
# while a full body is a hygiene problem.
MAX_ERROR_CHARS = 200

# Kinds Ladon applies that have no namespace. A document of any other kind that
# sets none would land in whatever namespace the kubectl context happens to
# point at, which is how an object ends up in the wrong place, so it is refused.
CLUSTER_SCOPED_KINDS = frozenset(
    {
        "ClusterSecretStore",
        "ClusterExternalSecret",
        "Namespace",
        "CustomResourceDefinition",
        "ClusterRole",
        "ClusterRoleBinding",
    }
)

# The ESO API group, its ExternalSecret CRD, and the jsonpath that names the
# version the cluster stores. Passed as argv, so the jsonpath carries no shell
# quoting of its own.
ESO_API_GROUP = "external-secrets.io"
ESO_CRD = f"externalsecrets.{ESO_API_GROUP}"
ESO_STORAGE_VERSION_JSONPATH = "jsonpath={.spec.versions[?(@.storage==true)].name}"

# The chart's own label on the operator deployment, and the label that records
# the version it was installed at.
ESO_NAME_SELECTOR = "app.kubernetes.io/name=external-secrets"
ESO_DEPLOYMENT_NAME = "external-secrets"
ESO_VERSION_LABEL = "app.kubernetes.io/version"
UNKNOWN_VERSION = "unknown"

# Fields a document may set at the top level that Ladon verifies. Everything
# else on a live object (status, server-set metadata, controller defaults) is
# not ours to compare.
VERIFIED_TOP_LEVEL_FIELDS = ("spec", "automountServiceAccountToken")
VERIFIED_METADATA_FIELDS = ("labels", "annotations")

# Subtrees where a subset comparison is not safe, so the live object may hold
# nothing Ladon did not set. Both of these are places where one added key widens
# access while every field Ladon wrote still reads back unchanged: a `namespaces`
# list beside a store's `namespaceSelector` admits a namespace Ladon never
# admitted, and a second entry in a provider's `auth` block is another way to
# authenticate. Everywhere else the subset rule stands, because a live object
# legitimately carries what the API server, a controller or another manager put
# there. Paths are dotted field paths, and strictness is inherited by everything
# below them, list entries included.
STRICT_PATHS = frozenset({"spec.conditions", "spec.provider.vault.auth"})

# kubectl prints one line per object: "<resource>/<name> <verb>", with a
# "(server dry run)" suffix on a server dry-run.
_RESULT_LINE = re.compile(
    r"^(?P<resource>[^\s/]+)/(?P<name>[^\s/]+)\s+(?P<verb>[a-z][a-z-]*)(?:\s+\(.*\))?$"
)

# Server-side apply reports one verb whether it created or updated the object,
# so the report cannot tell those apart; the read-back is what proves the state.
_VERB_ALIASES = {"serverside-applied": "configured", "patched": "configured"}

# The lines in kubectl's output that are errors. The tab matters on the third:
# kubectl indents the items of an aggregated error with a tab, while the help
# text it prints after a conflict uses bullets in column zero, and those are
# prose, not failures.
_ERROR_LINE_PREFIXES = ("error:", "Error from server")
_ERROR_ITEM_PREFIX = "\t* "

# What makes an error line a field-manager conflict rather than a refusal. Both
# quote kubectl's own wording; a message that merely uses the word "conflicts"
# (an admission webhook explaining a policy, say) matches neither.
_CONFLICT_PATTERNS = (
    re.compile(r"Apply failed with \d+ conflicts?"),
    re.compile(r'conflict with "'),
)

_MISSING = object()


class K8sError(RuntimeError):
    """A kubectl invocation failed, or kubectl is unavailable."""


class ClusterApplyError(K8sError):
    """An apply failed after documents were already written.

    Carries the report of what the apply did, so a caller can print it before
    the error: the write happened, and an operator who cannot see what landed
    has to go and look by hand.
    """

    def __init__(self, message: str, *, report: ApplyReport):
        super().__init__(message)
        self.report = report


class ForeignObjectError(WriteRefused):
    """The live object is there, and Ladon is not among its field managers.

    A refusal rather than an error: the cluster is reachable and the object is
    exactly what was named, and Ladon still will not remove something it did
    not create. Server-side apply records who owns what, so this is knowable
    rather than guessed, and the message names the managers that do own it so
    an operator knows who to ask.
    """


class ESONotInstalledError(K8sError):
    """The External Secrets CRDs are not on the cluster.

    Its own type because it is the one failure a caller may answer by
    installing the chart; every other probe failure means stop.
    """


@dataclass(frozen=True)
class ApplyResult:
    """What happened to one document: created, configured, unchanged, or blocked."""

    kind: str
    namespace: str | None
    name: str
    action: str
    reason: str = ""

    @property
    def target(self) -> str:
        """``kind/namespace/name``, or ``kind/name`` for a cluster-scoped kind."""
        if self.namespace:
            return f"{self.kind}/{self.namespace}/{self.name}"
        return f"{self.kind}/{self.name}"

    def __str__(self) -> str:
        suffix = f"({self.reason})" if self.reason else ""
        return f"{self.target}: {self.action}{suffix}"


@dataclass(frozen=True)
class ApplyReport:
    """The per-document outcome of one apply, in the order the documents were sent."""

    results: tuple[ApplyResult, ...] = ()
    dry_run: bool = True

    @property
    def blocked(self) -> tuple[ApplyResult, ...]:
        return tuple(r for r in self.results if r.action == BLOCKED)

    def lines(self) -> list[str]:
        return [str(r) for r in self.results]

    def __str__(self) -> str:
        return "\n".join(self.lines())


@dataclass(frozen=True)
class ESOInstall:
    """An External Secrets Operator already running on the cluster (D18a)."""

    namespace: str
    service_account: str
    version: str


def run(argv: Sequence[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a kubectl command, returning the completed process.

    Unlike ``ladon.install.helm.run`` this does not raise on a non-zero exit: a
    failed apply may still be a per-document conflict the caller reports rather
    than an error, so classifying the failure is the caller's job. A missing
    binary is still an error here, because no caller can do anything with it.
    """
    if shutil.which(argv[0]) is None:
        raise K8sError(f"{argv[0]!r} not found on PATH")
    try:
        return subprocess.run(  # noqa: S603
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise K8sError(f"{' '.join(argv[:3])} timed out after {DEFAULT_TIMEOUT_SECONDS}s") from exc


Runner = Callable[..., subprocess.CompletedProcess[str]]


def apply_argv(*, field_manager: str = DEFAULT_FIELD_MANAGER, dry_run: bool = True) -> list[str]:
    """Build the ``kubectl apply`` command. Never carries ``--force-conflicts``."""
    argv = ["kubectl", "apply", "--server-side", f"--field-manager={field_manager}"]
    if dry_run:
        argv.append("--dry-run=server")
    argv += ["-f", "-"]
    return argv


def get_argv(doc: dict) -> list[str]:
    """Build the ``kubectl get`` command that reads one document's live object."""
    _, namespace, name = _identify(doc)
    argv = ["kubectl", "get", f"{_resource_token(doc)}/{name}"]
    if namespace:
        argv += ["-n", namespace]
    return argv + ["-o", "json"]


def apply_documents(
    docs: Iterable[dict],
    *,
    field_manager: str = DEFAULT_FIELD_MANAGER,
    dry_run: bool = True,
    run: Runner = run,
) -> ApplyReport:
    """Apply documents as one YAML stream and report what happened to each.

    Defaults to a server dry-run, matching the dry-run-first rule the rest of
    the apply layer follows.

    A non-zero exit is classified per error line, not by scanning the whole
    output: every error line must be a field-manager conflict for the failure to
    become blocked documents. One refusal sharing the output with a conflict
    (an RBAC denial, an admission webhook) makes the whole apply an error, and
    so does an exit whose reason has no recognisable error line at all. The
    apply did not do what it was asked, and a report of blocked documents would
    say the wrong thing about why.

    A document kubectl printed a result line for was applied; the ones with no
    line are the ones that did not land, and they carry the conflict as their
    reason. Result lines carry no namespace, so a verb answers for at most as
    many documents of that kind and name as there were lines, in stream order.
    """
    docs = [_checked(d) for d in docs]
    if not docs:
        return ApplyReport(results=(), dry_run=dry_run)

    result = run(
        apply_argv(field_manager=field_manager, dry_run=dry_run),
        stdin=yaml.safe_dump_all(docs, sort_keys=False, default_flow_style=False),
    )
    reported = _parse_apply_output(result.stdout)

    blocked_reason = ""
    if result.returncode != 0:
        message = result.stderr or result.stdout
        if not _is_only_conflicts(message):
            raise K8sError(f"kubectl apply failed ({result.returncode}): {_first_line(message)}")
        blocked_reason = _first_line(message)

    results = []
    for doc in docs:
        kind, namespace, name = _identify(doc)
        verbs = reported.get((_resource_token(doc), name))
        if verbs:
            results.append(ApplyResult(kind, namespace, name, verbs.pop(0)))
        else:
            results.append(
                ApplyResult(
                    kind,
                    namespace,
                    name,
                    BLOCKED,
                    blocked_reason or "kubectl reported no result",
                )
            )
    return ApplyReport(results=tuple(results), dry_run=dry_run)


def converge_documents(
    docs: Iterable[dict],
    *,
    field_manager: str = DEFAULT_FIELD_MANAGER,
    dry_run: bool = True,
    run: Runner = run,
) -> ApplyReport:
    """Read, diff, write and verify a set of documents against the cluster.

    Every document is read back before anything is written. One that verifies
    clean is reported ``already current`` and is kept out of the apply stream
    entirely, so a second run writes nothing at all; one whose live object is
    absent is a ``create``; one that differs is an ``update``. The report names
    every document in the order it was given, whether or not it was sent.

    On an apply, what was written is read back again and verified, and a
    mismatch is an error naming the kind, the object and the field. A document
    kubectl refused keeps its ``blocked`` result and is not verified: it was
    never written. A dry run verifies nothing, because nothing landed.
    """
    docs = [_checked(doc) for doc in docs]
    actions = [_needed_action(doc, run=run) for doc in docs]
    pending = [index for index, action in enumerate(actions) if action != ALREADY_CURRENT]

    report = apply_documents(
        [docs[index] for index in pending],
        field_manager=field_manager,
        dry_run=dry_run,
        run=run,
    )
    applied = dict(zip(pending, report.results, strict=True))

    results: list[ApplyResult] = []
    landed: list[dict] = []
    for index, doc in enumerate(docs):
        kind, namespace, name = _identify(doc)
        outcome = applied.get(index)
        if outcome is None:
            results.append(ApplyResult(kind, namespace, name, ALREADY_CURRENT))
        elif outcome.action == BLOCKED:
            results.append(outcome)
        else:
            # Ladon's own verb, not kubectl's: the read-back above is what knows
            # whether this was a create or an update.
            results.append(ApplyResult(kind, namespace, name, actions[index]))
            landed.append(doc)

    report = ApplyReport(results=tuple(results), dry_run=dry_run)
    if not dry_run and landed:
        # Past this point the documents are written, so every failure carries
        # the report: what landed is what a caller has to be told about.
        try:
            problems = verify(landed, read_back(landed, run=run))
        except K8sError as exc:
            raise ClusterApplyError(str(exc), report=report) from exc
        if problems:
            raise ClusterApplyError(
                "the cluster did not store what Ladon applied: " + "; ".join(problems),
                report=report,
            )
    return report


def delete_argv(doc: dict) -> list[str]:
    """Build the ``kubectl delete`` command for one document's object.

    Always ``--ignore-not-found``: the object is read before this runs, and
    between the read and the delete somebody else may have removed it. That
    is the outcome asked for, not a failure.

    Always ``--wait=false`` too. kubectl's default is to poll until the object
    is gone, which turns one finalizer that is not making progress into a
    request held open for the whole timeout. The read-back this module does
    afterwards is what proves the outcome, so there is nothing for kubectl's
    wait to add except the blocking.
    """
    _, namespace, name = _identify(doc)
    argv = ["kubectl", "delete", f"{_resource_token(doc)}/{name}"]
    if namespace:
        argv += ["-n", namespace]
    return argv + ["--ignore-not-found", "--wait=false"]


def field_managers(live: dict) -> tuple[str, ...]:
    """Every manager that owns part of a live object, once each, in order.

    Server-side apply writes one ``managedFields`` entry per manager and
    operation, so a manager can appear more than once; this reports each once.
    An object with no entries at all belongs to nobody Ladon can name, and the
    empty tuple is what says so.

    Entries scoped to a subresource are left out. A controller writing an
    object's ``status`` records itself there, and it does not thereby own the
    object: ESO stamps every ExternalSecret Ladon applies, and reading that as
    ownership would make ESO the owner of everything on the cluster. Ladon
    writes no subresource, so nothing it owns is lost by this.
    """
    entries = (live.get("metadata") or {}).get("managedFields") or []
    seen: dict[str, None] = {}
    for entry in entries:
        manager = entry.get("manager")
        if manager and not entry.get("subresource"):
            seen.setdefault(str(manager), None)
    return tuple(seen)


def require_managed(doc: dict, live: dict, field_manager: str = DEFAULT_FIELD_MANAGER) -> None:
    """Refuse an object Ladon is not recorded as a manager of.

    Public because both directions need it. A delete reads it immediately
    before removing the object; an apply that would take fields from whoever
    holds them needs the same answer, and server-side apply cannot give it,
    since an apply by the same manager name is indistinguishable from the
    manager doing its job.
    """
    managers = field_managers(live)
    if field_manager in managers:
        return
    kind, _, name = _identify(doc)
    raise ForeignObjectError(
        f"{kind}/{name} is managed by "
        f"{', '.join(managers) if managers else 'no recorded manager'}, "
        f"not by {field_manager!r}; Ladon does not delete what it did not apply"
    )


def _is_terminating(live: dict) -> bool:
    """True when the API server has accepted a delete and is finishing it.

    An object with a ``deletionTimestamp`` is on its way out and is only still
    readable because a finalizer has not run yet. The delete did what it was
    asked; waiting for the controller is not this call's job.
    """
    return bool((live.get("metadata") or {}).get("deletionTimestamp"))


def delete_documents(
    docs: Iterable[dict],
    *,
    field_manager: str = DEFAULT_FIELD_MANAGER,
    dry_run: bool = True,
    run: Runner = run,
) -> ApplyReport:
    """Remove the objects these documents name, and report what happened to each.

    Only the identity of a document matters here: kind, name and namespace. The
    spec is not read, so a caller removing an object it created earlier does not
    have to be able to render it again.

    Each object is read, checked and deleted in that order, one document at a
    time. The check does not get its own earlier pass on purpose: whether Ladon
    manages an object is only true as of the moment it was read, and a pass
    that cleared every document first would be deciding on state that another
    manager could have taken over by the time the delete ran. Read, check,
    delete, with nothing in between, is the narrowest that window gets.

    The cost is that a refusal partway through a list leaves the documents
    before it deleted. With one document, which is what every caller here
    sends, there is no partway; with several, a ``ForeignObjectError`` still
    stops the rest.

    An object that is not there is reported ``absent`` and no delete runs, so
    asking twice is not an error. After a delete the object is read again: the
    result is ``delete`` when it is gone or is terminating with a
    ``deletionTimestamp``, and a still-present object raises, because kubectl
    accepting a delete is not the same as the cluster having done it.

    Defaults to a dry run, like every other write surface. A dry run does the
    read and the ownership check, so it answers the same refusals an apply
    would, and runs no delete.
    """
    docs = [_checked(doc) for doc in docs]
    results: list[ApplyResult] = []
    for doc in docs:
        kind, namespace, name = _identify(doc)
        live = read_one(doc, missing_ok=True, run=run)
        if live is None:
            results.append(ApplyResult(kind, namespace, name, ABSENT))
            continue
        require_managed(doc, live, field_manager)
        if dry_run:
            results.append(ApplyResult(kind, namespace, name, DELETE))
            continue
        outcome = run(delete_argv(doc))
        if outcome.returncode != 0:
            message = outcome.stderr or outcome.stdout
            raise K8sError(
                f"deleting {kind}/{name} failed ({outcome.returncode}): {_first_line(message)}"
            )
        after = read_one(doc, missing_ok=True, run=run)
        if after is not None and not _is_terminating(after):
            raise K8sError(
                f"{kind}/{name} is still on the cluster after kubectl accepted the delete"
            )
        results.append(ApplyResult(kind, namespace, name, DELETE))
    return ApplyReport(results=tuple(results), dry_run=dry_run)


def read_one(doc: dict, *, missing_ok: bool = False, run: Runner = run) -> dict | None:
    """Read one document's live object, or None when it is absent.

    ``missing_ok`` is the difference between a diff and a verification: before a
    write, an absent object is the reason to create it; after one, it means the
    write did not land.
    """
    kind, _, name = _identify(_checked(doc))
    result = run(get_argv(doc))
    if result.returncode != 0:
        message = result.stderr or result.stdout
        if missing_ok and NOT_FOUND_REASON in message:
            return None
        raise K8sError(
            f"reading back {kind}/{name} failed ({result.returncode}): {_first_line(message)}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise K8sError(f"reading back {kind}/{name} returned output that is not JSON") from exc


def read_back(docs: Iterable[dict], *, run: Runner = run) -> list[dict]:
    """Read the live object for each document, in the order given.

    An object that is not there is a failure here, not a None: this is the
    verification step, and a document with no live object did not land.
    """
    return [read_one(doc, run=run) for doc in docs]


def verify(docs: Iterable[dict], live: Iterable[dict]) -> list[str]:
    """Compare only the fields the documents set against the live objects.

    Returns one string per mismatch, naming the kind, the object and the field
    path and never a value. A live object may carry anything else the API server
    or a controller added; only what Ladon asked for is checked. Maps are
    compared as subsets (key order is meaningless), lists element by element in
    order, with a length difference reported: an extra entry in a store's
    ``conditions`` widens who may use it, so it is drift, not noise.

    Under :data:`STRICT_PATHS` the subset rule is off and a key the document
    does not set is itself a mismatch, because inside those subtrees an added
    key widens access while everything Ladon wrote still reads back unchanged.
    """
    index = {_identify(obj): obj for obj in live}
    problems: list[str] = []
    for doc in docs:
        kind, namespace, name = _identify(_checked(doc))
        target = f"{kind}/{name}"
        obj = index.get((kind, namespace, name))
        if obj is None:
            problems.append(f"{target}: not present in the live objects read back")
            continue
        for key in VERIFIED_TOP_LEVEL_FIELDS:
            if key in doc:
                problems += _compare(
                    doc[key], obj.get(key, _MISSING), f"{target}: {key}", field=key
                )
        desired_meta = doc.get("metadata", {})
        live_meta = obj.get("metadata", {})
        for key in VERIFIED_METADATA_FIELDS:
            if key in desired_meta:
                problems += _compare(
                    desired_meta[key],
                    live_meta.get(key, _MISSING),
                    f"{target}: metadata.{key}",
                    field=f"metadata.{key}",
                )
    return problems


def eso_api_version(*, run: Runner = run) -> str:
    """Return the ESO CRD version the cluster stores (``v1`` or ``v1beta1``).

    The served versions differ across ESO major versions, and rendering a
    resource at a version the cluster does not store is the API mismatch
    deployment-order.md warns about. Raises when the CRDs are absent: that is a
    cluster without an operator, not a cluster on an older version.

    One CRD is read and its answer is taken for the group: an operator installs
    its custom resource definitions as a unit, so ``ExternalSecret`` and
    ``ClusterSecretStore`` are stored at the same version wherever one operator
    owns them, which is what D18a keeps true. Probe per kind if a deployment
    ever splits that ownership.
    """
    resources = run(["kubectl", "api-resources", f"--api-group={ESO_API_GROUP}", "-o", "name"])
    if resources.returncode != 0:
        raise K8sError(
            f"probing the {ESO_API_GROUP} API group failed ({resources.returncode}): "
            f"{_first_line(resources.stderr or resources.stdout)}"
        )
    if not resources.stdout.strip():
        raise ESONotInstalledError(
            f"the External Secrets CRDs are not installed on this cluster "
            f"(the {ESO_API_GROUP} API group is empty)"
        )
    crd = run(["kubectl", "get", "crd", ESO_CRD, "-o", ESO_STORAGE_VERSION_JSONPATH])
    if crd.returncode != 0:
        raise K8sError(
            f"reading the {ESO_CRD} CRD failed ({crd.returncode}): "
            f"{_first_line(crd.stderr or crd.stdout)}"
        )
    versions = crd.stdout.split()
    if not versions:
        raise K8sError(f"the {ESO_CRD} CRD names no storage version")
    return versions[0]


def detect_eso(*, run: Runner = run) -> ESOInstall | None:
    """Find an External Secrets Operator already running on the cluster (D18a).

    ESO is cluster-scoped and singular: two operators fight over one set of
    CRDs. Returns what is installed so the caller can reuse it, or None when the
    cluster has none. A kubectl failure is an error, never a None: treating "I
    could not look" as "nothing is there" is how a second operator gets
    installed. More than one match is an error too, for the same reason: the
    cluster is already in the state this function exists to prevent, and
    choosing one of them would hide it.
    """
    result = run(["kubectl", "get", "deploy", "-A", "-l", ESO_NAME_SELECTOR, "-o", "json"])
    if result.returncode != 0:
        raise K8sError(
            f"looking for an existing external-secrets operator failed ({result.returncode}): "
            f"{_first_line(result.stderr or result.stdout)}"
        )
    if not result.stdout.strip():
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise K8sError("the operator lookup returned output that is not JSON") from exc
    items = payload.get("items", [])
    if not items:
        return None
    if len(items) > 1:
        raise K8sError(
            f"found {len(items)} deployments labelled {ESO_NAME_SELECTOR} on this cluster; "
            f"external-secrets is cluster-scoped and singular, so resolve by hand"
        )
    item = items[0]
    pod_spec = item.get("spec", {}).get("template", {}).get("spec", {})
    return ESOInstall(
        namespace=item.get("metadata", {}).get("namespace", ""),
        service_account=pod_spec.get("serviceAccountName", "default"),
        version=_operator_version(item),
    )


# Internals -----------------------------------------------------------------


def _checked(doc: dict) -> dict:
    """Refuse a document Ladon cannot place exactly.

    Every report line needs a kind and a name. A namespace is required too
    unless the kind is one of the cluster-scoped ones: an implicit namespace is
    whatever the kubectl context points at, and Ladon does not write objects
    into a namespace it did not name.
    """
    metadata = doc.get("metadata") or {}
    kind = doc.get("kind")
    name = metadata.get("name")
    if not kind or not name:
        raise K8sError("every document must set kind and metadata.name")
    if not metadata.get("namespace") and kind not in CLUSTER_SCOPED_KINDS:
        raise K8sError(
            f"{kind}/{name} sets no metadata.namespace and {kind} is not a cluster-scoped "
            f"kind; name the namespace rather than inheriting the kubectl context's"
        )
    return doc


def _needed_action(doc: dict, *, run: Runner) -> str:
    """What one document needs: a create, an update, or nothing at all."""
    live = read_one(doc, missing_ok=True, run=run)
    if live is None:
        return CREATE
    return UPDATE if verify([doc], [live]) else ALREADY_CURRENT


def _identify(doc: dict) -> tuple[str, str | None, str]:
    metadata = doc.get("metadata") or {}
    return doc.get("kind", ""), metadata.get("namespace"), metadata.get("name", "")


def _resource_token(doc: dict) -> str:
    """The token kubectl uses for a kind: lowercase kind, plus group if any."""
    kind = doc.get("kind", "").lower()
    group = doc.get("apiVersion", "").rpartition("/")[0]
    return f"{kind}.{group}" if group else kind


def _parse_apply_output(stdout: str) -> dict[tuple[str, str], list[str]]:
    """Collect kubectl's per-object verbs, in order, per (resource token, name).

    A list rather than one verb: the lines carry no namespace, so the count is
    how many documents of that kind and name the output can answer for.
    """
    reported: dict[tuple[str, str], list[str]] = {}
    for line in stdout.splitlines():
        match = _RESULT_LINE.match(line.strip())
        if not match:
            continue
        verb = _VERB_ALIASES.get(match["verb"], match["verb"])
        reported.setdefault((match["resource"], match["name"]), []).append(verb)
    return reported


def _error_lines(message: str) -> list[str]:
    """The lines of kubectl output that report a failure, not prose."""
    return [
        line.strip()
        for line in message.splitlines()
        if line.startswith(_ERROR_LINE_PREFIXES) or line.startswith(_ERROR_ITEM_PREFIX)
    ]


def _is_only_conflicts(message: str) -> bool:
    """True when every error line is a field-manager conflict.

    Fails closed: a failure with no recognisable error line is not a conflict,
    and neither is one whose lines are a mix.
    """
    lines = _error_lines(message)
    return bool(lines) and all(
        any(pattern.search(line) for pattern in _CONFLICT_PATTERNS) for line in lines
    )


def _first_line(text: str) -> str:
    """At most the first non-empty line, truncated.

    A multi-line body cannot reach a log, and neither can a long one: kubectl
    can echo an object into an error.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if len(stripped) > MAX_ERROR_CHARS:
            return f"{stripped[:MAX_ERROR_CHARS]} (truncated)"
        return stripped
    return "no output"


def _compare(desired: Any, live: Any, path: str, *, field: str, strict: bool = False) -> list[str]:
    """Compare one field, reporting paths and never values.

    ``path`` is what a message says; ``field`` is the dotted field path the
    strict rule is keyed on, which list indexes do not extend, so strictness
    covers every entry of a strict list alike.
    """
    if live is _MISSING:
        return [f"{path} is missing from the live object"]
    if isinstance(desired, dict):
        if not isinstance(live, dict):
            return [f"{path} has a different shape in the live object"]
        problems: list[str] = []
        if strict:
            problems += [f"{path}.{key} is not set by Ladon" for key in live if key not in desired]
        for key, value in desired.items():
            child = f"{field}.{key}" if field else key
            problems += _compare(
                value,
                live.get(key, _MISSING),
                f"{path}.{key}",
                field=child,
                strict=strict or child in STRICT_PATHS,
            )
        return problems
    if isinstance(desired, list):
        if not isinstance(live, list):
            return [f"{path} has a different shape in the live object"]
        if len(desired) != len(live):
            return [f"{path} has {_entries(len(desired))}, the live object has {len(live)}"]
        problems = []
        for index, (want, got) in enumerate(zip(desired, live, strict=True)):
            problems += _compare(want, got, f"{path}[{index}]", field=field, strict=strict)
        return problems
    if desired != live:
        return [f"{path} differs from the live object"]
    return []


def _entries(count: int) -> str:
    return f"{count} entry" if count == 1 else f"{count} entries"


def _operator_version(item: dict) -> str:
    """The version label if the chart set one, else the image tag, else unknown."""
    labelled = item.get("metadata", {}).get("labels", {}).get(ESO_VERSION_LABEL)
    if labelled:
        return labelled
    containers = item.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
    if not containers:
        return UNKNOWN_VERSION
    # The operator's own container, not a sidecar somebody added beside it.
    operator = next((c for c in containers if c.get("name") == ESO_DEPLOYMENT_NAME), containers[0])
    # Drop a digest, then read the tag off the final path segment only, so a
    # registry host carrying a port is not mistaken for a tag.
    reference = operator.get("image", "").split("@", 1)[0]
    last_segment = reference.rsplit("/", 1)[-1]
    return last_segment.rsplit(":", 1)[1] if ":" in last_segment else UNKNOWN_VERSION
