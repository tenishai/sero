"""A recording fake of the OpenBao client surface the apply layer uses.

It serves canned live state, records reads and writes separately (so a test can
assert that a dry-run wrote nothing), and folds each write back into its own
state, so a read-back after an apply sees what was written. Shapes mirror hvac:
mount and auth listings are keyed by ``"<path>/"`` and wrapped in ``data``, a
path read returns ``{"data": {...}}``, a namespace listing returns its children
under ``keys``, and a missing object raises :class:`InvalidPath` or reads as
``None``.

State is kept per OpenBao namespace, because that is the isolation the model
rests on: a mount in one namespace is a different object from the same mount
name in another. The namespace in force is the one on ``adapter.namespace``,
which is the attribute hvac turns into the ``X-Vault-Namespace`` header, so a
test can assert that every read and write of a namespaced op carried it.
"""

from __future__ import annotations


class InvalidPath(Exception):
    """Mirrors hvac's 404 exception, which the executor identifies by name."""

    status_code = 404


class FakeAdapter:
    """The ``client.adapter`` surface: just the namespace header hvac sends."""

    def __init__(self, namespace=None):
        self.namespace = namespace


class NamespaceState:
    """Everything one OpenBao namespace holds."""

    def __init__(self, *, mounts=None, auth_methods=None, policies=None, paths=None):
        self.mounts = dict(mounts or {})
        self.auth_methods = dict(auth_methods or {})
        self.policies = dict(policies or {})
        self.paths = {k: dict(v) for k, v in (paths or {}).items()}
        self.children = set()


class FakeSys:
    """The ``client.sys`` surface: mounts, auth methods, policies, namespaces."""

    def __init__(self, bao: FakeOpenBao):
        self._bao = bao

    def list_mounted_secrets_engines(self):
        self._bao.record_read("list_mounted_secrets_engines")
        return {"data": dict(self._bao.state().mounts)}

    def list_auth_methods(self):
        self._bao.record_read("list_auth_methods")
        return {"data": dict(self._bao.state().auth_methods)}

    def read_policy(self, *, name):
        self._bao.record_read("read_policy", name)
        policies = self._bao.state().policies
        if name not in policies:
            raise InvalidPath(f"no policy named {name}")
        return {"data": {"name": name, "rules": policies[name]}}

    def list_namespaces(self):
        self._bao.record_read("list_namespaces")
        children = self._bao.state().children
        if not children:
            # OpenBao answers 404 for a namespace with no children, the same way
            # it does for a path that does not exist.
            raise InvalidPath("no namespaces")
        return {"data": {"keys": sorted(f"{name}/" for name in children)}}

    def enable_secrets_engine(self, *, backend_type, path, options=None):
        self._bao.record_call("enable_secrets_engine", path, backend_type, options)
        self._bao.state().mounts[f"{path}/"] = {
            "type": backend_type,
            "options": dict(options or {}),
        }

    def enable_auth_method(self, *, method_type, path):
        self._bao.record_call("enable_auth_method", path, method_type)
        self._bao.state().auth_methods[f"{path}/"] = {"type": method_type}

    def create_or_update_policy(self, *, name, policy):
        self._bao.record_call("create_or_update_policy", name, policy)
        self._bao.state().policies[name] = policy

    def create_namespace(self, *, path):
        self._bao.record_call("create_namespace", path)
        self._bao.state().children.add(path)


class FakeOpenBao:
    """Records calls instead of talking to OpenBao, and serves live state.

    The constructor's state seeds the namespace the client starts in (the root,
    unless ``namespace`` says otherwise); other namespaces start empty, as they
    do on a fresh OpenBao.
    """

    def __init__(
        self,
        *,
        mounts=None,
        auth_methods=None,
        policies=None,
        paths=None,
        namespaces=(),
        namespace=None,
    ):
        self.adapter = FakeAdapter(namespace)
        root = NamespaceState(
            mounts=mounts,
            auth_methods=auth_methods,
            policies=policies,
            paths=paths,
        )
        root.children = set(namespaces)
        self._home = self._key(namespace)
        self._states = {self._home: root}
        self.calls = []
        self.reads = []
        self.sys = FakeSys(self)

    # The namespace in force, and the state it holds ---------------------------

    @staticmethod
    def _key(namespace):
        return (namespace or "").strip("/")

    def state(self, namespace=None) -> NamespaceState:
        """The state of ``namespace``, defaulting to the one in force."""
        key = self._key(namespace if namespace is not None else self.adapter.namespace)
        return self._states.setdefault(key, NamespaceState())

    def record_read(self, *args):
        self.reads.append((*args, self.adapter.namespace))

    def record_call(self, *args):
        self.calls.append((*args, self.adapter.namespace))

    # Convenience accessors for the namespace the client was built in ----------

    @property
    def home(self) -> NamespaceState:
        """The state of the namespace the client was constructed in."""
        return self._states[self._home]

    @property
    def mounts(self):
        return self.home.mounts

    @property
    def auth_methods(self):
        return self.home.auth_methods

    @property
    def policies(self):
        return self.home.policies

    @property
    def paths(self):
        return self.home.paths

    @property
    def namespaces(self):
        return self.home.children

    # Path reads and writes ----------------------------------------------------

    def read(self, path):
        self.record_read("read", path)
        data = self.state().paths.get(path)
        return None if data is None else {"data": dict(data)}

    def write_data(self, path, *, data):
        self.record_call("write_data", path, data)
        self.state().paths.setdefault(path, {}).update(data)
