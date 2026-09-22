"""Render the baseline OpenBao policy templates (D3b).

These are the generic policy shapes from docs/baseline.md, rendered per namespace
from parameters (the KV mount path). Rendering is pure: it produces the policy
HCL as a string. Applying it to OpenBao is a separate step (the SDK layer).

KV v2 splits a mount into data, metadata, and soft-delete paths, so the policies
target those explicitly rather than a single glob.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_KV_MOUNT = "kv"


@dataclass(frozen=True)
class Policy:
    """A named OpenBao policy and its HCL body."""

    name: str
    hcl: str

    @classmethod
    def from_blocks(cls, name: str, blocks: list[str]) -> Policy:
        """Build a Policy from rendered HCL path blocks, joined into one body."""
        return cls(name=name, hcl="\n\n".join(blocks) + "\n")


def path_block(path: str, capabilities: list[str]) -> str:
    """One HCL path grant. Shared, so every policy renderer grants in one dialect."""
    caps = ", ".join(f'"{c}"' for c in capabilities)
    return f'path "{path}" {{\n  capabilities = [{caps}]\n}}'


def team_readwrite(mount: str = DEFAULT_KV_MOUNT) -> Policy:
    """Full read/write on a team's KV v2 mount.

    Grants create/read/update/delete/list on secrets, and the soft-delete
    lifecycle, scoped to the one mount. It does not grant cross-namespace or
    system access; the team namespace already isolates it.
    """
    blocks = [
        path_block(f"{mount}/data/*", ["create", "read", "update", "delete", "list"]),
        path_block(f"{mount}/metadata/*", ["read", "list", "delete"]),
        path_block(f"{mount}/delete/*", ["update"]),
        path_block(f"{mount}/undelete/*", ["update"]),
        path_block(f"{mount}/destroy/*", ["update"]),
    ]
    return Policy.from_blocks("team-readwrite", blocks)


def team_readonly(mount: str = DEFAULT_KV_MOUNT) -> Policy:
    """Read and list only, on a team's KV v2 mount."""
    blocks = [
        path_block(f"{mount}/data/*", ["read", "list"]),
        path_block(f"{mount}/metadata/*", ["read", "list"]),
    ]
    return Policy.from_blocks("team-readonly", blocks)


def external_secrets_read(mount: str = DEFAULT_KV_MOUNT) -> Policy:
    """Read access for the External Secrets Operator: KV data and metadata only.

    This is the narrowest grant that lets ESO read the secrets it syncs. It
    grants no write and no system access.
    """
    blocks = [
        path_block(f"{mount}/data/*", ["read"]),
        path_block(f"{mount}/metadata/*", ["read", "list"]),
    ]
    return Policy.from_blocks("external-secrets-read", blocks)


def external_secrets_root(mount: str = DEFAULT_KV_MOUNT) -> Policy:
    """Cross-namespace read for a centralized ESO that pushes to cloud SMs.

    The centralized External Secrets Operator runs in the platform root and reads
    secrets out of every team namespace to push them to GCP Secret Manager or AWS
    Secrets Manager. The ``+`` matches exactly one namespace segment, so this
    grants read across all team namespaces without granting anything else. Enable
    it only when cloud secret-manager sync is turned on.
    """
    blocks = [
        path_block(f"+/{mount}/data/*", ["read"]),
        path_block(f"+/{mount}/metadata/*", ["read", "list"]),
    ]
    return Policy.from_blocks("external-secrets-root", blocks)


def baseline_team_policies(mount: str = DEFAULT_KV_MOUNT) -> list[Policy]:
    """The per-team baseline policies rendered for one namespace's KV mount."""
    return [team_readwrite(mount), team_readonly(mount)]
