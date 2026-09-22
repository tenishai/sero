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
and controller status never read as drift.

Two probes support the ESO work (D18a): ``eso_api_version`` reports which CRD
version the cluster stores, and ``detect_eso`` finds an operator somebody else
already installed so Ladon can reuse it instead of fighting it for the CRDs.

Secrets hygiene: these documents carry references (a service account, a JWT
role, a KV path), never values. Error messages name the kind, the object and the
field path, and carry at most the first line of kubectl's stderr, so a message
that happens to echo an object body cannot reach a log.
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

DEFAULT_FIELD_MANAGER = "ladon"

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

# kubectl prints one line per object: "<resource>/<name> <verb>", with a
# "(server dry run)" suffix on a server dry-run.
_RESULT_LINE = re.compile(
    r"^(?P<resource>[^\s/]+)/(?P<name>[^\s/]+)\s+(?P<verb>[a-z][a-z-]*)(?:\s+\(.*\))?$"
)

# Server-side apply reports one verb whether it created or updated the object,
# so the report cannot tell those apart; the read-back is what proves the state.
_VERB_ALIASES = {"serverside-applied": "configured", "patched": "configured"}

# A non-zero exit carrying either of these is a field-manager conflict, which
# blocks one document rather than failing the whole apply.
_CONFLICT_MARKERS = ("conflict", "field manager")

_MISSING = object()


class K8sError(RuntimeError):
    """A kubectl invocation failed, or kubectl is unavailable."""


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
        return tuple(r for r in self.results if r.action == "blocked")

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
    return subprocess.run(argv, input=stdin, capture_output=True, text=True)  # noqa: S603


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
    the apply layer follows. A field-manager conflict blocks the document it
    names and leaves the rest reported; any other failure is an error.
    """
    docs = [_checked(d) for d in docs]
    if not docs:
        return ApplyReport(results=(), dry_run=dry_run)

    result = run(
        apply_argv(field_manager=field_manager, dry_run=dry_run),
        stdin=yaml.safe_dump_all(docs, sort_keys=False, default_flow_style=False),
    )
    reported = _parse_apply_output(result.stdout)

    conflict_reason = ""
    named_in_conflict: set[str] = set()
    if result.returncode != 0:
        message = result.stderr or result.stdout
        if not _looks_like_conflict(message):
            raise K8sError(f"kubectl apply failed ({result.returncode}): {_first_line(message)}")
        conflict_reason = _first_line(message)
        # kubectl names the object it could not apply in the conflict message.
        # Whatever it names is blocked; when it names nothing, the documents it
        # reported no result for are the ones that did not land.
        named_in_conflict = {_identify(d)[2] for d in docs if _identify(d)[2] in message}

    results = []
    for doc in docs:
        kind, namespace, name = _identify(doc)
        verb = reported.get((_resource_token(doc), name)) or reported.get((None, name))
        if verb is not None and name not in named_in_conflict:
            results.append(ApplyResult(kind, namespace, name, verb))
        elif conflict_reason:
            results.append(ApplyResult(kind, namespace, name, "blocked", conflict_reason))
        else:
            results.append(
                ApplyResult(kind, namespace, name, "blocked", "kubectl reported no result")
            )
    return ApplyReport(results=tuple(results), dry_run=dry_run)


def read_back(docs: Iterable[dict], *, run: Runner = run) -> list[dict]:
    """Read the live object for each document, in the order given."""
    live = []
    for doc in docs:
        kind, _, name = _identify(_checked(doc))
        result = run(get_argv(doc))
        if result.returncode != 0:
            raise K8sError(
                f"reading back {kind}/{name} failed ({result.returncode}): "
                f"{_first_line(result.stderr or result.stdout)}"
            )
        try:
            live.append(json.loads(result.stdout))
        except json.JSONDecodeError as exc:
            raise K8sError(f"reading back {kind}/{name} returned output that is not JSON") from exc
    return live


def verify(docs: Iterable[dict], live: Iterable[dict]) -> list[str]:
    """Compare only the fields the documents set against the live objects.

    Returns one string per mismatch, naming the kind, the object and the field
    path and never a value. A live object may carry anything else the API server
    or a controller added; only what Ladon asked for is checked. Maps are
    compared as subsets (key order is meaningless), lists element by element in
    order, with a length difference reported: an extra entry in a store's
    ``conditions`` widens who may use it, so it is drift, not noise.
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
                problems += _compare(doc[key], obj.get(key, _MISSING), f"{target}: {key}")
        desired_meta = doc.get("metadata", {})
        live_meta = obj.get("metadata", {})
        for key in VERIFIED_METADATA_FIELDS:
            if key in desired_meta:
                problems += _compare(
                    desired_meta[key],
                    live_meta.get(key, _MISSING),
                    f"{target}: metadata.{key}",
                )
    return problems


def eso_api_version(*, run: Runner = run) -> str:
    """Return the ESO CRD version the cluster stores (``v1`` or ``v1beta1``).

    The served versions differ across ESO major versions, and rendering a
    resource at a version the cluster does not store is the API mismatch
    deployment-order.md warns about. Raises when the CRDs are absent: that is a
    cluster without an operator, not a cluster on an older version.
    """
    resources = run(["kubectl", "api-resources", f"--api-group={ESO_API_GROUP}", "-o", "name"])
    if resources.returncode != 0:
        raise K8sError(
            f"probing the {ESO_API_GROUP} API group failed ({resources.returncode}): "
            f"{_first_line(resources.stderr or resources.stdout)}"
        )
    if not resources.stdout.strip():
        raise K8sError(
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
    installed.
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
    item = _operator_deployment(items)
    pod_spec = item.get("spec", {}).get("template", {}).get("spec", {})
    return ESOInstall(
        namespace=item.get("metadata", {}).get("namespace", ""),
        service_account=pod_spec.get("serviceAccountName", "default"),
        version=_operator_version(item),
    )


# Internals -----------------------------------------------------------------


def _checked(doc: dict) -> dict:
    """Refuse a document that cannot be named; every report line needs a name."""
    metadata = doc.get("metadata") or {}
    if not doc.get("kind") or not metadata.get("name"):
        raise K8sError("every document must set kind and metadata.name")
    return doc


def _identify(doc: dict) -> tuple[str, str | None, str]:
    metadata = doc.get("metadata") or {}
    return doc.get("kind", ""), metadata.get("namespace"), metadata.get("name", "")


def _resource_token(doc: dict) -> str:
    """The token kubectl uses for a kind: lowercase kind, plus group if any."""
    kind = doc.get("kind", "").lower()
    group = doc.get("apiVersion", "").rpartition("/")[0]
    return f"{kind}.{group}" if group else kind


def _parse_apply_output(stdout: str) -> dict[tuple[str | None, str], str]:
    """Index kubectl's per-object lines by (resource token, name) and by name."""
    reported: dict[tuple[str | None, str], str] = {}
    for line in stdout.splitlines():
        match = _RESULT_LINE.match(line.strip())
        if not match:
            continue
        verb = _VERB_ALIASES.get(match["verb"], match["verb"])
        reported[(match["resource"], match["name"])] = verb
        reported.setdefault((None, match["name"]), verb)
    return reported


def _looks_like_conflict(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _CONFLICT_MARKERS)


def _first_line(text: str) -> str:
    """The first non-empty line, so a multi-line body cannot reach a log."""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return "no output"


def _compare(desired: Any, live: Any, path: str) -> list[str]:
    if live is _MISSING:
        return [f"{path} is missing from the live object"]
    if isinstance(desired, dict):
        if not isinstance(live, dict):
            return [f"{path} has a different shape in the live object"]
        problems: list[str] = []
        for key, value in desired.items():
            problems += _compare(value, live.get(key, _MISSING), f"{path}.{key}")
        return problems
    if isinstance(desired, list):
        if not isinstance(live, list):
            return [f"{path} has a different shape in the live object"]
        if len(desired) != len(live):
            return [f"{path} has {_entries(len(desired))}, the live object has {len(live)}"]
        problems = []
        for index, (want, got) in enumerate(zip(desired, live, strict=True)):
            problems += _compare(want, got, f"{path}[{index}]")
        return problems
    if desired != live:
        return [f"{path} differs from the live object"]
    return []


def _entries(count: int) -> str:
    return f"{count} entry" if count == 1 else f"{count} entries"


def _operator_deployment(items: list[dict]) -> dict:
    """Prefer the controller deployment by name; fall back to the first match."""
    for item in items:
        if item.get("metadata", {}).get("name") == ESO_DEPLOYMENT_NAME:
            return item
    return items[0]


def _operator_version(item: dict) -> str:
    """The version label if the chart set one, else the image tag, else unknown."""
    labelled = item.get("metadata", {}).get("labels", {}).get(ESO_VERSION_LABEL)
    if labelled:
        return labelled
    containers = item.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
    if not containers:
        return UNKNOWN_VERSION
    # Drop a digest, then read the tag off the final path segment only, so a
    # registry host carrying a port is not mistaken for a tag.
    reference = containers[0].get("image", "").split("@", 1)[0]
    last_segment = reference.rsplit("/", 1)[-1]
    return last_segment.rsplit(":", 1)[1] if ":" in last_segment else UNKNOWN_VERSION
