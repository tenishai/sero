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
        raise AssertionError(f"unexpected kubectl command: {argv}")

    @property
    def applies(self) -> list[list[str]]:
        return [call for call in self.calls if call[1] == "apply"]

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
                self.objects[key] = self._mutate(doc) if self._mutate else copy.deepcopy(doc)
            lines.append(f"{key[2]} {verb}{suffix}\n")
        return Result(
            argv,
            returncode=1 if refused else 0,
            stdout="".join(lines),
            stderr=CONFLICT if refused else "",
        )
