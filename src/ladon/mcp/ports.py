"""The read port the MCP tools read secrets through (D12).

The read-only tools need to list a user's namespaces, list the secret paths in a
namespace, and inspect one path's key names. Rather than depend on a live
OpenBao, they depend on this ``SecretReader`` protocol. The production
implementation wraps the OpenBao client; tests pass a fake. This keeps the tool
logic (auth gating, masking, audit) testable without a server and separable from
the OpenBao integration.

The port is read-only and value-free by contract: a secret's *keys* are returned
already masked (``ladon.hygiene.mask``), and a plaintext value never crosses this
boundary. Implementations must enforce that; the tools never ask for a value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class SecretPath:
    """One secret path in a namespace: its path and how many keys it holds."""

    path: str
    key_count: int


@dataclass(frozen=True)
class SecretDetail:
    """One secret path's state in one environment, with keys already masked.

    ``masked_keys`` maps each key name to a masked preview (from
    ``ladon.hygiene.mask``); it never contains a plaintext value. ``exists`` is
    false when the path is absent in that environment, in which case the other
    fields are empty.
    """

    namespace: str
    path: str
    env: str
    exists: bool
    version: int | None = None
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


class SecretReader(Protocol):
    """Read-only access to namespaces and secrets for the MCP read tools.

    A production implementation wraps the OpenBao client; a fake backs the tests.
    All methods are scoped to a single authenticated user's reachable data; the
    tools enforce authorization before calling, and the reader enforces it again.
    """

    def list_namespaces(self, user_email: str) -> list[str]:
        """Namespaces the user may read."""
        ...

    def list_secrets(self, user_email: str, namespace: str) -> list[SecretPath]:
        """Secret paths in ``namespace`` (path and key count only, no values)."""
        ...

    def get_secret(self, user_email: str, namespace: str, path: str, env: str) -> SecretDetail:
        """One path's state in one environment, with keys already masked."""
        ...


class SecretWriter(Protocol):
    """Write access to secrets for the MCP write tools.

    A production implementation wraps the OpenBao client; a fake backs the tests.
    The values passed in are written to OpenBao and never returned or logged by
    the tools; only key names appear in the ``WriteResult``. ``update`` merges:
    keys in ``values`` are written, keys in ``remove_keys`` are deleted, and any
    other existing key is left untouched.
    """

    def create_secret(
        self, user_email: str, namespace: str, path: str, env: str, values: dict[str, str]
    ) -> WriteResult:
        """Create a new secret path with ``values``."""
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
