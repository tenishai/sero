"""Tests for the OpenBao client-construction layer (D3b).

The credential types and verification are exercised with fakes that mimic the
hvac client surface, so these run with no live OpenBao. The real hvac.Client is
patched out where construction would otherwise build one.
"""

import pytest

from ladon.apply import client as client_mod
from ladon.apply.client import (
    AppRoleAuth,
    BaoConnection,
    BaoConnectionError,
    JwtAuth,
    TokenAuth,
    connect,
)
from ladon.hygiene import PlaceholderSecretError


class FakeApprole:
    def __init__(self, calls):
        self._calls = calls

    def login(self, *, role_id, secret_id, mount_point):
        self._calls.append(("approle_login", role_id, mount_point))


class FakeJwt:
    def __init__(self, calls):
        self._calls = calls

    def jwt_login(self, *, role, jwt, path):
        self._calls.append(("jwt_login", role, path))


class FakeAuth:
    def __init__(self, calls):
        self.approle = FakeApprole(calls)
        self.jwt = FakeJwt(calls)


class FakeSys:
    def __init__(self, calls, healthy=True):
        self._calls = calls
        self._healthy = healthy

    def read_health_status(self, *, method):
        self._calls.append(("read_health", method))
        if not self._healthy:
            raise RuntimeError("sealed")


class FakeClient:
    """Mimics the slice of hvac.Client the client layer uses."""

    def __init__(self, *, url, namespace, verify, timeout, session, authed=True, healthy=True):
        self.url = url
        self.namespace = namespace
        self.verify = verify
        self.timeout = timeout
        self.token = None
        self.calls: list = []
        self.auth = FakeAuth(self.calls)
        self.sys = FakeSys(self.calls, healthy=healthy)
        self._authed = authed

    def is_authenticated(self):
        return self._authed


@pytest.fixture
def patch_client(monkeypatch):
    """Patch hvac.Client construction; return a holder for the built fake."""
    holder = {}

    def factory(authed=True, healthy=True):
        def _make(**kwargs):
            c = FakeClient(authed=authed, healthy=healthy, **kwargs)
            holder["client"] = c
            return c

        monkeypatch.setattr(client_mod.hvac, "Client", _make)
        return holder

    return factory


def test_token_auth_sets_token_and_verifies(patch_client):
    holder = patch_client()
    conn = BaoConnection(address="https://bao.example:8200", namespace="root")
    c = connect(conn, TokenAuth(token="s.realtoken123"))
    assert c.token == "s.realtoken123"
    assert c.namespace == "root"
    # Verification ran: is_authenticated + a health read, no writes.
    assert ("read_health", "GET") in holder["client"].calls


def test_token_placeholder_refused(patch_client):
    patch_client()
    conn = BaoConnection(address="https://bao.example:8200")
    with pytest.raises(PlaceholderSecretError):
        connect(conn, TokenAuth(token="changeme"))


def test_approle_login_dispatched(patch_client):
    holder = patch_client()
    conn = BaoConnection(address="https://bao.example:8200")
    connect(conn, AppRoleAuth(role_id="r-123", secret_id="s.secret-abc"))
    assert ("approle_login", "r-123", "approle") in holder["client"].calls


def test_approle_placeholder_secret_id_refused(patch_client):
    patch_client()
    conn = BaoConnection(address="https://bao.example:8200")
    with pytest.raises(PlaceholderSecretError):
        connect(conn, AppRoleAuth(role_id="r-123", secret_id="TODO"))


def test_jwt_login_dispatched(patch_client):
    holder = patch_client()
    conn = BaoConnection(address="https://bao.example:8200")
    connect(conn, JwtAuth(role="eso", jwt="header.payload.sig", path="jwt-cluster"))
    assert ("jwt_login", "eso", "jwt-cluster") in holder["client"].calls


def test_not_authenticated_fails_fast(patch_client):
    patch_client(authed=False)
    conn = BaoConnection(address="https://bao.example:8200")
    with pytest.raises(BaoConnectionError, match="not authenticated"):
        connect(conn, TokenAuth(token="s.realtoken123"))


def test_health_failure_surfaces(patch_client):
    patch_client(healthy=False)
    conn = BaoConnection(address="https://bao.example:8200")
    with pytest.raises(BaoConnectionError, match="health check failed"):
        connect(conn, TokenAuth(token="s.realtoken123"))


def test_check_false_skips_live_verification(patch_client):
    holder = patch_client(authed=False, healthy=False)
    conn = BaoConnection(address="https://bao.example:8200")
    # Would fail verification, but check=False skips it entirely.
    c = connect(conn, TokenAuth(token="s.realtoken123"), check=False)
    assert c.token == "s.realtoken123"
    assert ("read_health", "GET") not in holder["client"].calls


def test_connection_passes_tls_and_timeout(patch_client):
    holder = patch_client()
    conn = BaoConnection(
        address="https://bao.example:8200",
        tls_verify="/etc/ssl/ca.pem",
        timeout=10,
    )
    connect(conn, TokenAuth(token="s.realtoken123"))
    built = holder["client"]
    assert built.verify == "/etc/ssl/ca.pem"
    assert built.timeout == 10


def test_connection_from_env_reads_address(monkeypatch):
    monkeypatch.delenv("BAO_ADDR", raising=False)
    monkeypatch.setenv("VAULT_ADDR", "https://env-bao.example:8200")
    conn = BaoConnection.from_env()
    assert conn.address == "https://env-bao.example:8200"


def test_connection_from_env_requires_address(monkeypatch):
    for name in ("BAO_ADDR", "VAULT_ADDR"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(BaoConnectionError, match="no OpenBao address"):
        BaoConnection.from_env()


def test_token_from_env(monkeypatch):
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    monkeypatch.setenv("BAO_TOKEN", "s.envtoken")
    assert TokenAuth.from_env().token == "s.envtoken"


def test_token_from_env_requires_value(monkeypatch):
    for name in ("BAO_TOKEN", "VAULT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(BaoConnectionError, match="no token"):
        TokenAuth.from_env()
