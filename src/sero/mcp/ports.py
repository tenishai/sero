"""The read port the MCP tools read secrets through (D12).

The read-only tools need to list a user's namespaces, list the secret paths in a
namespace, and inspect one path's key names. Rather than depend on a live
OpenBao, they depend on this ``SecretReader`` protocol. The production
implementation wraps the OpenBao client; tests pass a fake. This keeps the tool
logic (auth gating, masking, audit) testable without a server and separable from
the OpenBao integration.

The port is read-only and value-free by contract: a secret's *keys* are returned
already masked (``sero.hygiene.mask``), and a plaintext value never crosses this
boundary. Implementations must enforce that; the tools never ask for a value.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from sero.hygiene import WriteRefused


@dataclass(frozen=True)
class SecretPath:
    """One secret path in a namespace: its path and how many keys it holds."""

    path: str
    key_count: int


@dataclass(frozen=True)
class SecretListing:
    """The secret paths in one namespace, and whether that is all of them.

    ``truncated`` is the honest half of a bounded walk. KV v2 lists one level
    at a time, so finding every leaf means following folders, and an
    implementation that follows them without limit hands one namespace the
    ability to keep a request busy. The bounds are therefore real, and a
    listing that hit one is not the whole namespace: saying so is the
    difference between a caller seeing everything and a caller believing it
    has.
    """

    secrets: tuple[SecretPath, ...] = ()
    truncated: bool = False


@dataclass(frozen=True)
class SecretDetail:
    """One secret path's state in one environment, with keys already masked.

    ``masked_keys`` maps each key name to a masked preview (from
    ``sero.hygiene.mask``); it never contains a plaintext value. ``exists`` is
    false when the path is absent in that environment, in which case the other
    fields are empty.

    ``updated_at`` is when the current version was written, as the store
    reports it, or None when it does not. It is a timestamp, so it says when a
    path last changed without saying anything about what it holds.
    """

    namespace: str
    path: str
    env: str
    exists: bool
    version: int | None = None
    updated_at: str | None = None
    masked_keys: dict[str, str] = field(default_factory=dict)

    @property
    def key_count(self) -> int:
        return len(self.masked_keys)


@dataclass(frozen=True)
class WriteResult:
    """The outcome of a secret write: what changed, by name, never a value.

    ``created`` is true for a new path, false for an update. ``written_keys`` and
    ``removed_keys`` are key names only; a value never appears here.
    """

    namespace: str
    path: str
    env: str
    created: bool
    written_keys: list[str] = field(default_factory=list)
    removed_keys: list[str] = field(default_factory=list)


class SecretPathTaken(WriteRefused):
    """A create lost a race: the path was there by the time the write ran.

    Every surface checks that a path is absent before creating one, and the
    check and the write are two calls. A ``WriteRefused``, because it is a
    decision and not a failure: nothing was overwritten, and the caller's next
    move is to read what is there and update it instead.
    """


class SecretReader(Protocol):
    """Read-only access to namespaces and secrets for the MCP read tools.

    A production implementation wraps the OpenBao client; a fake backs the tests.
    All methods are scoped to a single authenticated user's reachable data; the
    tools enforce authorization before calling, and the reader enforces it again.
    """

    def list_namespaces(self, user_email: str) -> list[str]:
        """Namespaces the user may read."""
        ...

    def list_secrets(self, user_email: str, namespace: str) -> SecretListing:
        """Secret paths in ``namespace`` (path and key count only, no values).

        The listing says whether it is complete. An implementation that bounds
        its walk must report ``truncated``, since a caller cannot tell a
        namespace with forty secrets from one with four hundred otherwise.
        """
        ...

    def get_secret(self, user_email: str, namespace: str, path: str, env: str) -> SecretDetail:
        """One path's state in one environment, with keys already masked."""
        ...


def require_non_empty_merge(
    *,
    namespace: str,
    path: str,
    current_keys: Iterable[str],
    values: Iterable[str],
    remove_keys: Iterable[str],
) -> None:
    """Refuse a merge that would leave a path with no keys.

    Part of the ``SecretWriter`` contract rather than one implementation's
    private check, so a second writer cannot quietly lose the rule, and so the
    dry-run can preview the same refusal the apply would raise. Takes key names
    only; a value never reaches it.

    An empty secret still exists and reads as a path with nothing in it, which
    is almost never what was meant. Deleting the path is a separate, explicit
    operation.
    """
    remaining = (set(current_keys) | set(values)) - set(remove_keys)
    if not remaining:
        raise WriteRefused(f"refusing to leave {namespace}/{path} empty; delete the path instead")


class SecretWriter(Protocol):
    """Write access to secrets for the MCP write tools.

    A production implementation wraps the OpenBao client; a fake backs the tests.
    The values passed in are written to OpenBao and never returned or logged by
    the tools; only key names appear in the ``WriteResult``. ``update`` merges:
    keys in ``values`` are written, keys in ``remove_keys`` are deleted, and any
    other existing key is left untouched.

    An implementation of ``update_secret`` must call
    :func:`require_non_empty_merge` before it writes: emptying a path is refused,
    and the rule belongs to every writer, not to one of them.
    """

    def create_secret(
        self, user_email: str, namespace: str, path: str, env: str, values: dict[str, str]
    ) -> WriteResult:
        """Create a new secret path with ``values``.

        Creates, never overwrites: the surfaces above check that the path is
        absent first, and an implementation must make the write itself
        conditional on that still being true, raising
        :class:`SecretPathTaken` when it is not. Two calls arriving together
        would otherwise have one silently replace the other's secret.
        """
        ...

    def update_secret(
        self,
        user_email: str,
        namespace: str,
        path: str,
        env: str,
        values: dict[str, str],
        remove_keys: list[str],
    ) -> WriteResult:
        """Merge ``values`` into an existing path and delete ``remove_keys``."""
        ...


class SecretEraser(Protocol):
    """Removing a whole secret path, which no other port does.

    Its own protocol rather than a third method on ``SecretWriter`` because the
    two are not the same permission. A writer merges keys into a path and is
    forbidden from emptying one (:func:`require_non_empty_merge`); this removes
    the path and every version of it, which is the operation that rule points
    at. A surface that may write is not automatically a surface that may erase,
    and keeping them apart is what lets a deployment wire only one of them.
    """

    def delete_secret(self, user_email: str, namespace: str, path: str) -> None:
        """Remove a path and all of its versions, and confirm it is gone.

        Idempotent, and unconditional with it: a path that is already absent is
        not an error, so there is nothing for an implementation to gain by
        checking first. Whether anything was there is what the caller's own
        read says, which is why nothing is returned; an implementation that
        cannot confirm the path is gone afterwards raises.

        No ``env``, because a deployment serves one and a delete has nothing to
        label. Returns no key names; the caller read those before it asked.
        """
        ...
