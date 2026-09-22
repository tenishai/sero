"""A fake cluster behind the kubectl seam, for the apply-layer tests (D18).

Every helper in ``ladon.install.k8s`` takes its subprocess ``run`` as a keyword
argument, so a test hands it this object instead of a shell. It answers
``kubectl get`` from what has been applied to it and prints the per-object lines
``kubectl apply`` prints, which is what makes a second converge over the same
documents observably a no-op rather than a canned string.

The documents it holds carry references (a service account, a role, a KV path),
never a value, exactly like the real ones.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterable

import yaml

from ladon.install.k8s import get_argv

# What the API server says when the object is not there. Ladon reads the reason
# code, so the rest of the line is only shape.
NOT_FOUND = 'Error from server (NotFound): {resource} "{name}" not found'

# One field-manager conflict, in kubectl's own wording: the shape that makes a
# document blocked rather than an error.
CONFLICT = 'error: Apply failed with 1 conflict: conflict with "other-manager"\n'


class Result:
    """The subset of CompletedProcess the k8s helpers read."""

    def __init__(self, argv: list[str], *, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.args = list(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeCluster:
    """kubectl against an in-memory cluster.

    ``live`` seeds objects that already exist. ``blocked`` names objects the
    cluster refuses with a field-manager conflict. ``mutate`` rewrites a document
    as it is stored, which is how a test makes an apply land as something other
    than what was sent.
    """

    def __init__(
        self,
        *,
        live: Iterable[dict] = (),
        blocked: Iterable[str] = (),
        mutate: Callable[[dict], dict] | None = None,
    ):
        self.objects: dict[tuple[str, ...], dict] = {
            self.key(doc): copy.deepcopy(doc) for doc in live
        }
        self.calls: list[list[str]] = []
        self.stdins: list[str | None] = []
        self._blocked = set(blocked)
        self._mutate = mutate

    @staticmethod
    def key(doc: dict) -> tuple[str, ...]:
        """The identity of one object: the same argv a read of it would use."""
        return tuple(get_argv(doc))

    def __call__(self, argv, *, stdin=None) -> Result:
        argv = list(argv)
        self.calls.append(argv)
        self.stdins.append(stdin)
        if argv[1] == "get":
            return self._get(argv)
        if argv[1] == "apply":
            return self._apply(argv, stdin)
        if argv[1] == "delete":
            return self._delete(argv)
        raise AssertionError(f"unexpected kubectl command: {argv}")

    @property
    def applies(self) -> list[list[str]]:
        return [call for call in self.calls if call[1] == "apply"]

    @property
    def deletes(self) -> list[list[str]]:
        return [call for call in self.calls if call[1] == "delete"]

    @property
    def streams(self) -> list[str]:
        """The YAML stream sent to each apply, in order."""
        return [
            stdin for call, stdin in zip(self.calls, self.stdins, strict=True) if call[1] == "apply"
        ]

    def stored(self, doc: dict) -> dict | None:
        return self.objects.get(self.key(doc))

    def _get(self, argv: list[str]) -> Result:
        obj = self.objects.get(tuple(argv))
        if obj is None:
            resource, _, name = argv[2].partition("/")
            return Result(
                argv, returncode=1, stderr=NOT_FOUND.format(resource=resource, name=name) + "\n"
            )
        return Result(argv, stdout=json.dumps(obj))

    def _apply(self, argv: list[str], stdin: str | None) -> Result:
        dry_run = "--dry-run=server" in argv
        suffix = " (server dry run)" if dry_run else ""
        lines: list[str] = []
        refused = False
        for doc in yaml.safe_load_all(stdin or ""):
            if doc["metadata"]["name"] in self._blocked:
                refused = True
                continue
            key = self.key(doc)
            verb = "configured" if key in self.objects else "created"
            if not dry_run:
                stored = self._mutate(doc) if self._mutate else copy.deepcopy(doc)
                self.objects[key] = _managed_by(stored, _manager(argv))
            lines.append(f"{key[2]} {verb}{suffix}\n")
        return Result(
            argv,
            returncode=1 if refused else 0,
            stdout="".join(lines),
            stderr=CONFLICT if refused else "",
        )

    def _delete(self, argv: list[str]) -> Result:
        target = argv[2]
        namespace = argv[argv.index("-n") + 1] if "-n" in argv else None
        key = self._key_for(target, namespace)
        if key is None:
            if "--ignore-not-found" in argv:
                return Result(argv)
            resource, _, name = target.partition("/")
            return Result(
                argv, returncode=1, stderr=NOT_FOUND.format(resource=resource, name=name) + "\n"
            )
        del self.objects[key]
        return Result(argv, stdout=f"{target} deleted\n")

    def _key_for(self, target: str, namespace: str | None) -> tuple[str, ...] | None:
        """The stored object a delete's argv names, by resource, name and namespace."""
        for key in self.objects:
            if key[2] != target:
                continue
            stored = key[key.index("-n") + 1] if "-n" in key else None
            if stored == namespace:
                return key
        return None


def _manager(argv: list[str]) -> str:
    """The field manager an apply argv carries."""
    flag = "--field-manager="
    return next((a[len(flag) :] for a in argv if a.startswith(flag)), "kubectl")


def _managed_by(doc: dict, manager: str) -> dict:
    """Stamp a stored object the way server-side apply records its owner.

    The real API server writes one ``managedFields`` entry per manager, and the
    delete path reads them to decide whether an object is Ladon's. Without this
    the fake would answer that question for nobody.
    """
    metadata = doc.setdefault("metadata", {})
    entries = metadata.setdefault("managedFields", [])
    if not any(entry.get("manager") == manager for entry in entries):
        entries.append({"manager": manager, "operation": "Apply"})
    return doc
