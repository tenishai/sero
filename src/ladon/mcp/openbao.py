"""Live OpenBao-backed implementations of the MCP read and write ports (D12).

This is the thin wrapper that connects the MCP tools to a real OpenBao, the way
``ladon.apply.executor`` is the thin wrapper for the apply layer. The tool logic,
hygiene, and auth all live above the ports; here we only translate a port call
into KV v2 API calls against a namespace-scoped client.

Two collaborators are injected, so the wrapper stays testable with a fake hvac
client and free of any connection concern:

* ``client_for(namespace)`` returns an ``hvac`` client scoped to a user's OpenBao
  namespace (built by ``ladon.apply.client.connect`` with that namespace).
* ``namespaces_for(user_email)`` returns the namespaces a user may access. How a
  deployment maps users to namespaces (an identity-provider group lookup, a
  config file) is pluggable; this layer does not assume a provider.

One deployment serves one environment, so ``env`` never picks a client: see
:class:`OpenBaoSecrets`.

The read path masks every value through ``ladon.hygiene.mask`` before returning,
so the port's value-free contract holds at the boundary. The write path takes
values and writes them, but returns only key names. ``reveal_value`` is the one
deliberate exception, for the reveal route only; it is named so that a search
for plaintext exits finds exactly one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ladon.hygiene import mask
from ladon.mcp.ports import (
    SecretDetail,
    SecretListing,
    SecretPath,
    WriteResult,
    require_non_empty_merge,
)

# KV v2 mount the tools operate on within a namespace. Matches the baseline.
DEFAULT_KV_MOUNT = "kv"

# Bounds on the folder walk in `list_secrets`: deep enough for the platform's
# `apps/<app>/<purpose>` layout with room to spare, small enough that a tree
# somebody wrote by hand cannot turn one listing into thousands of reads.
MAX_LIST_DEPTH = 6
MAX_LISTED_SECRETS = 500


def _is_not_found(exc: BaseException) -> bool:
    """hvac raises `InvalidPath` for a 404; an empty KV listing is one."""
    return type(exc).__name__ == "InvalidPath" or getattr(exc, "status_code", None) == 404


class SecretNotDeleted(RuntimeError):
    """OpenBao accepted a delete and the path is still readable.

    Not a refusal the caller can act on: Ladon said it would remove a path,
    the store answered, and the secret is still there. It surfaces as a 500
    and an audit row, which is the honest answer, because the alternative is
    reporting a destroy that did not happen.
    """


ClientFor = Callable[[str], Any]
NamespacesFor = Callable[[str], list[str]]


class OpenBaoSecrets:
    """Read and write secrets in OpenBao for the MCP tools.

    Implements both ``SecretReader`` and ``SecretWriter``. The KV path a tool
    passes is used directly under the ``kv/`` mount of the user's namespace.

    ``env`` selects nothing here, and must not: one Ladon serves one OpenBao,
    which holds one environment. It travels with a call so results can say which
    environment they describe, and the surfaces above refuse a call naming an
    environment this deployment does not serve. Treating it as a selector is
    what would let two environments share one set of paths.
    """

    def __init__(
        self,
        *,
        client_for: ClientFor,
        namespaces_for: NamespacesFor,
        mount: str = DEFAULT_KV_MOUNT,
    ) -> None:
        self._client_for = client_for
        self._namespaces_for = namespaces_for
        self._mount = mount

    # ── SecretReader ─────────────────────────────────────────────────────

    def list_namespaces(self, user_email: str) -> list[str]:
        return list(self._namespaces_for(user_email))

    def list_secrets(self, user_email: str, namespace: str) -> SecretListing:
        """Every leaf secret in the namespace, by full path.

        KV v2 lists one level at a time and marks folders with a trailing
        slash, so this walks the folders down (bounded, so a runaway tree
        cannot pin the request) and reports leaves only. An empty mount is a
        404 from OpenBao; here it is an empty list, because a namespace with
        nothing in it yet is the ordinary state of a new team.

        Both bounds are reported rather than applied quietly. Stopping at
        :data:`MAX_LISTED_SECRETS` leaves secrets unlisted, and declining to
        descend past :data:`MAX_LIST_DEPTH` leaves a whole subtree unlisted,
        and a caller told neither would read a short list as a small namespace.
        """
        client = self._client_for(namespace)
        out: list[SecretPath] = []
        truncated = False
        pending: list[tuple[str, int]] = [("", 0)]
        while pending and len(out) < MAX_LISTED_SECRETS:
            prefix, depth = pending.pop(0)
            for key in self._list_level(client, prefix):
                full = f"{prefix}{key}"
                if key.endswith("/"):
                    if depth + 1 < MAX_LIST_DEPTH:
                        pending.append((full, depth + 1))
                    else:
                        # A folder this walk will not open. Whatever is under
                        # it is real and is not in this answer.
                        truncated = True
                    continue
                if len(out) >= MAX_LISTED_SECRETS:
                    truncated = True
                    break
                detail = self._read(client, namespace, full, env="")
                out.append(SecretPath(path=full, key_count=detail.key_count))
        # Folders still queued when the count ran out hold secrets too.
        truncated = truncated or bool(pending)
        out.sort(key=lambda p: p.path)
        return SecretListing(secrets=tuple(out), truncated=truncated)

    def _list_level(self, client: Any, prefix: str) -> list[str]:
        try:
            listing = client.secrets.kv.v2.list_secrets(path=prefix, mount_point=self._mount)
        except Exception as exc:
            if _is_not_found(exc):
                return []
            raise
        keys = listing.get("data", {}).get("keys", []) if listing else []
        return [str(k) for k in keys]

    def get_secret(self, user_email: str, namespace: str, path: str, env: str) -> SecretDetail:
        client = self._client_for(namespace)
        return self._read(client, namespace, path, env=env)

    def _read(self, client: Any, namespace: str, path: str, *, env: str) -> SecretDetail:
        """One path's state, or ``exists=False`` when OpenBao says it is absent.

        Only a 404 is absence. Every other failure is raised: a denial, an
        expired token, a vault that is sealed or unreachable all mean Ladon
        could not look, and reporting that as "there is nothing there" is how a
        read answers 404 for a secret that exists and a delete reports success
        over a path it never touched.
        """
        try:
            resp = client.secrets.kv.v2.read_secret_version(
                path=path, mount_point=self._mount, raise_on_deleted_version=False
            )
        except Exception as exc:
            if not _is_not_found(exc):
                raise
            return SecretDetail(namespace=namespace, path=path, env=env, exists=False)
        if not resp:
            return SecretDetail(namespace=namespace, path=path, env=env, exists=False)
        data = resp.get("data", {})
        raw = data.get("data") or {}
        metadata = data.get("metadata") or {}
        version = metadata.get("version")
        # When the current version was written. A time, not a value: it says a
        # path changed, never what it changed to.
        created = metadata.get("created_time")
        # Mask at the boundary: only masked previews leave this module.
        masked = {k: mask(str(v)) for k, v in raw.items()}
        return SecretDetail(
            namespace=namespace,
            path=path,
            env=env,
            exists=True,
            version=version,
            updated_at=str(created) if created else None,
            masked_keys=masked,
        )

    # ── SecretWriter ─────────────────────────────────────────────────────

    def create_secret(
        self, user_email: str, namespace: str, path: str, env: str, values: dict[str, str]
    ) -> WriteResult:
        client = self._client_for(namespace)
        client.secrets.kv.v2.create_or_update_secret(
            path=path, secret=dict(values), mount_point=self._mount
        )
        return WriteResult(
            namespace=namespace,
            path=path,
            env=env,
            created=True,
            written_keys=list(values),
        )

    def update_secret(
        self,
        user_email: str,
        namespace: str,
        path: str,
        env: str,
        values: dict[str, str],
        remove_keys: list[str],
    ) -> WriteResult:
        client = self._client_for(namespace)
        current = self._read_raw(client, path)
        require_non_empty_merge(
            namespace=namespace,
            path=path,
            current_keys=current,
            values=values,
            remove_keys=remove_keys,
        )
        merged = dict(current)
        merged.update(values)
        for key in remove_keys:
            merged.pop(key, None)
        client.secrets.kv.v2.create_or_update_secret(
            path=path, secret=merged, mount_point=self._mount
        )
        return WriteResult(
            namespace=namespace,
            path=path,
            env=env,
            created=False,
            written_keys=list(values),
            removed_keys=[k for k in remove_keys if k in current],
        )

    # ── SecretEraser ─────────────────────────────────────────────────────

    def delete_secret(self, user_email: str, namespace: str, path: str) -> None:
        """Remove a path and every version of it, then prove it is gone.

        Metadata and all versions, not the soft delete KV v2 does by default: a
        soft-deleted path still exists, still lists, and can be undeleted, which
        is not what a caller asking for the path to be gone means. The versions
        hold the values, so leaving them is leaving the secret.

        Unconditional: the metadata delete answers 204 whether or not the path
        was there, so there is nothing to gain by reading first and a race to
        lose by it. Whether anything was there is a question the caller's own
        read answers, which is why this returns nothing.

        The read-back is the part that matters. A delete Ladon reported and
        OpenBao did not perform is the failure worth catching here rather than
        the next time somebody reads the path and finds the credential still
        live, so a path that is still readable afterwards raises.
        """
        client = self._client_for(namespace)
        client.secrets.kv.v2.delete_metadata_and_all_versions(path=path, mount_point=self._mount)
        if self._read(client, namespace, path, env="").exists:
            raise SecretNotDeleted(
                f"{namespace}/{path} is still readable after OpenBao accepted the delete"
            )

    # ── Reveal (the one plaintext exit, gated above this layer) ──────────

    def reveal_value(self, user_email: str, namespace: str, path: str, env: str, key: str) -> str:
        """Return one key's plaintext value.

        The single call in this module that returns an unmasked value. It exists
        only for the reveal route, which is itself behind a gate that a
        deployment must switch on (the default refuses). The value is returned
        to the caller and never logged, stored, or put in an audit row.
        """
        client = self._client_for(namespace)
        current = self._read_raw(client, path)
        if key not in current:
            raise KeyError(f"no key {key!r} at {namespace}/{path}")
        return str(current[key])

    def _read_raw(self, client: Any, path: str) -> dict[str, str]:
        """Read the current plaintext for a merge. Never returned or logged.

        Only a 404 reads as an empty path, for the same reason as ``_read`` and
        with more at stake: an update merges into what this returns and writes
        the result back, so a denial or an unreachable vault swallowed here
        would merge into nothing and overwrite every key the path holds.
        """
        try:
            resp = client.secrets.kv.v2.read_secret_version(
                path=path, mount_point=self._mount, raise_on_deleted_version=False
            )
        except Exception as exc:
            if not _is_not_found(exc):
                raise
            return {}
        if not resp:
            return {}
        return dict((resp.get("data", {}) or {}).get("data") or {})
