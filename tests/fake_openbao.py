"""A recording fake of the OpenBao client surface the apply layer uses.

It serves canned live state, records reads and writes separately (so a test can
assert that a dry-run wrote nothing), and folds each write back into its own
state, so a read-back after an apply sees what was written. Shapes mirror hvac:
mount and auth listings are keyed by ``"<path>/"`` and wrapped in ``data``, a
path read returns ``{"data": {...}}``, and a missing object raises
:class:`InvalidPath` or reads as ``None``.
"""

from __future__ import annotations


class InvalidPath(Exception):
    """Mirrors hvac's 404 exception, which the executor identifies by name."""

    status_code = 404


class FakeSys:
    """The ``client.sys`` surface: mounts, auth methods, and policies."""

    def __init__(self, bao: FakeOpenBao):
        self._bao = bao

    def list_mounted_secrets_engines(self):
        self._bao.reads.append(("list_mounted_secrets_engines",))
        return {"data": dict(self._bao.mounts)}

    def list_auth_methods(self):
        self._bao.reads.append(("list_auth_methods",))
        return {"data": dict(self._bao.auth_methods)}

    def read_policy(self, *, name):
        self._bao.reads.append(("read_policy", name))
        if name not in self._bao.policies:
            raise InvalidPath(f"no policy named {name}")
        return {"data": {"name": name, "rules": self._bao.policies[name]}}

    def enable_secrets_engine(self, *, backend_type, path, options=None):
        self._bao.calls.append(("enable_secrets_engine", path, backend_type, options))
        self._bao.mounts[f"{path}/"] = {"type": backend_type, "options": dict(options or {})}

    def tune_mount_configuration(self, *, path, options=None):
        self._bao.calls.append(("tune_mount_configuration", path, options))
        self._bao.mounts[f"{path}/"]["options"] = dict(options or {})

    def enable_auth_method(self, *, method_type, path):
        self._bao.calls.append(("enable_auth_method", path, method_type))
        self._bao.auth_methods[f"{path}/"] = {"type": method_type}

    def create_or_update_policy(self, *, name, policy):
        self._bao.calls.append(("create_or_update_policy", name, policy))
        self._bao.policies[name] = policy


class FakeOpenBao:
    """Records calls instead of talking to OpenBao, and serves live state."""

    def __init__(self, *, mounts=None, auth_methods=None, policies=None, paths=None):
        self.mounts = dict(mounts or {})
        self.auth_methods = dict(auth_methods or {})
        self.policies = dict(policies or {})
        self.paths = {k: dict(v) for k, v in (paths or {}).items()}
        self.calls = []
        self.reads = []
        self.sys = FakeSys(self)

    def read(self, path):
        self.reads.append(("read", path))
        data = self.paths.get(path)
        return None if data is None else {"data": dict(data)}

    def write_data(self, path, *, data):
        self.calls.append(("write_data", path, data))
        self.paths.setdefault(path, {}).update(data)
