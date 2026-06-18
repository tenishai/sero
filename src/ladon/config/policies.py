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


def _block(path: str, capabilities: list[str]) -> str:
    caps = ", ".join(f'"{c}"' for c in capabilities)
    return f'path "{path}" {{\n  capabilities = [{caps}]\n}}'


def team_readwrite(mount: str = DEFAULT_KV_MOUNT) -> Policy:
    """Full read/write on a team's KV v2 mount.

    Grants create/read/update/delete/list on secrets, and the soft-delete
    lifecycle, scoped to the one mount. It does not grant cross-namespace or
    system access; the team namespace already isolates it.
    """
    blocks = [
        _block(f"{mount}/data/*", ["create", "read", "update", "delete", "list"]),
        _block(f"{mount}/metadata/*", ["read", "list", "delete"]),
        _block(f"{mount}/delete/*", ["update"]),
        _block(f"{mount}/undelete/*", ["update"]),
        _block(f"{mount}/destroy/*", ["update"]),
    ]
    return Policy(name="team-readwrite", hcl="\n\n".join(blocks) + "\n")


def team_readonly(mount: str = DEFAULT_KV_MOUNT) -> Policy:
    """Read and list only, on a team's KV v2 mount."""
    blocks = [
        _block(f"{mount}/data/*", ["read", "list"]),
        _block(f"{mount}/metadata/*", ["read", "list"]),
    ]
    return Policy(name="team-readonly", hcl="\n\n".join(blocks) + "\n")


def external_secrets_read(mount: str = DEFAULT_KV_MOUNT) -> Policy:
    """Read access for the External Secrets Operator: KV data and metadata only.

    This is the narrowest grant that lets ESO read the secrets it syncs. It
    grants no write and no system access.
    """
    blocks = [
        _block(f"{mount}/data/*", ["read"]),
        _block(f"{mount}/metadata/*", ["read", "list"]),
    ]
    return Policy(name="external-secrets-read", hcl="\n\n".join(blocks) + "\n")


def baseline_team_policies(mount: str = DEFAULT_KV_MOUNT) -> list[Policy]:
    """The per-team baseline policies rendered for one namespace's KV mount."""
    return [team_readwrite(mount), team_readonly(mount)]
