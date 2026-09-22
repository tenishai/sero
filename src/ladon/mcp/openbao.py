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
from ladon.mcp.ports import SecretDetail, SecretPath, WriteResult, require_non_empty_merge

# KV v2 mount the tools operate on within a namespace. Matches the baseline.
DEFAULT_KV_MOUNT = "kv"

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

    def list_secrets(self, user_email: str, namespace: str) -> list[SecretPath]:
        client = self._client_for(namespace)
        listing = client.secrets.kv.v2.list_secrets(path="", mount_point=self._mount)
        keys = listing.get("data", {}).get("keys", []) if listing else []
        out: list[SecretPath] = []
        for key in keys:
            if key.endswith("/"):
                # A folder, not a leaf secret; report it with an unknown count.
                out.append(SecretPath(path=key, key_count=0))
                continue
            detail = self._read(client, namespace, key, env="")
            out.append(SecretPath(path=key, key_count=detail.key_count))
        return out

    def get_secret(self, user_email: str, namespace: str, path: str, env: str) -> SecretDetail:
        client = self._client_for(namespace)
        return self._read(client, namespace, path, env=env)

    def _read(self, client: Any, namespace: str, path: str, *, env: str) -> SecretDetail:
        try:
            resp = client.secrets.kv.v2.read_secret_version(
                path=path, mount_point=self._mount, raise_on_deleted_version=False
            )
        except Exception:
            return SecretDetail(namespace=namespace, path=path, env=env, exists=False)
        if not resp:
            return SecretDetail(namespace=namespace, path=path, env=env, exists=False)
        data = resp.get("data", {})
        raw = data.get("data") or {}
        version = (data.get("metadata") or {}).get("version")
        # Mask at the boundary: only masked previews leave this module.
        masked = {k: mask(str(v)) for k, v in raw.items()}
        return SecretDetail(
            namespace=namespace,
            path=path,
            env=env,
            exists=True,
            version=version,
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
        """Read the current plaintext for a merge. Never returned or logged."""
        try:
            resp = client.secrets.kv.v2.read_secret_version(
                path=path, mount_point=self._mount, raise_on_deleted_version=False
            )
        except Exception:
            return {}
        if not resp:
            return {}
        return dict((resp.get("data", {}) or {}).get("data") or {})
