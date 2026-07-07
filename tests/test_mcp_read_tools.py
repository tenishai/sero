"""Tests for the MCP read-only tool logic (D12).

The *_impl functions are exercised with a fake SecretReader and an in-memory
datastore session, so no OpenBao and no running MCP server are needed. A separate
smoke test builds the FastMCP server and checks the tools register.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ladon.datastore import repositories as repo
from ladon.datastore.models import Base
from ladon.hygiene import mask
from ladon.mcp.ports import SecretDetail, SecretPath
from ladon.mcp.server import (
    AccessDeniedError,
    get_secret_impl,
    list_my_namespaces_impl,
    list_secrets_impl,
)

USER = "op@example.com"


class FakeReader:
    """A fake SecretReader: canned namespaces/paths, masked keys, no values."""

    def __init__(self):
        self.namespaces = {USER: ["team-a", "team-b"]}
        self.paths = {"team-a": [SecretPath("team-a/app", 2)]}
        # Real (unmasked) values live only here; get_secret returns masked.
        self._keys = {("team-a", "team-a/app", "dev"): {"API_KEY": "supersecret", "DB": "pw"}}

    def list_namespaces(self, user_email):
        return list(self.namespaces.get(user_email, []))

    def list_secrets(self, user_email, namespace):
        return list(self.paths.get(namespace, []))

    def get_secret(self, user_email, namespace, path, env):
        raw = self._keys.get((namespace, path, env))
        if raw is None:
            return SecretDetail(namespace=namespace, path=path, env=env, exists=False)
        return SecretDetail(
            namespace=namespace,
            path=path,
            env=env,
            exists=True,
            version=1,
            masked_keys={k: mask(v) for k, v in raw.items()},
        )


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def allowed(session):
    repo.allow(session, email=USER, added_by="admin@example.com")
    session.commit()


def test_list_namespaces_requires_allow_list(session):
    # Not on the allow list -> denied, and nothing is read.
    with pytest.raises(AccessDeniedError, match="allow list"):
        list_my_namespaces_impl(FakeReader(), session, user_email=USER)


def test_list_namespaces_ok_and_audited(session, allowed):
    result = list_my_namespaces_impl(FakeReader(), session, user_email=USER)
    assert result == {"namespaces": ["team-a", "team-b"]}
    calls = repo.recent_calls(session)
    assert len(calls) == 1
    assert calls[0].tool_name == "list_my_namespaces"
    assert calls[0].status == "ok"


def test_list_secrets_ok(session, allowed):
    result = list_secrets_impl(FakeReader(), session, user_email=USER, namespace="team-a")
    assert result["namespace"] == "team-a"
    assert result["secrets"] == [{"path": "team-a/app", "key_count": 2}]


def test_list_secrets_denies_foreign_namespace(session, allowed):
    with pytest.raises(AccessDeniedError, match="may not access namespace"):
        list_secrets_impl(FakeReader(), session, user_email=USER, namespace="team-z")


def test_get_secret_returns_masked_keys_only(session, allowed):
    result = get_secret_impl(
        FakeReader(), session, user_email=USER, namespace="team-a", path="team-a/app", env="dev"
    )
    assert result["exists"] is True
    assert result["key_count"] == 2
    # Masked, never the plaintext.
    assert "supersecret" not in str(result)
    assert result["masked_keys"]["API_KEY"].startswith("sup")
    assert result["masked_keys"]["API_KEY"].endswith("*")


def test_get_secret_absent_path(session, allowed):
    result = get_secret_impl(
        FakeReader(), session, user_email=USER, namespace="team-a", path="team-a/none", env="dev"
    )
    assert result["exists"] is False
    assert result["masked_keys"] == {}


def test_error_path_is_audited(session, allowed):
    class Boom(FakeReader):
        def list_secrets(self, user_email, namespace):
            raise RuntimeError("openbao unreachable")

    with pytest.raises(RuntimeError, match="unreachable"):
        list_secrets_impl(Boom(), session, user_email=USER, namespace="team-a")
    call = repo.recent_calls(session)[0]
    assert call.tool_name == "list_secrets"
    assert call.status == "error"
    assert "unreachable" in call.error


def test_build_server_registers_read_tools():
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    from ladon.mcp.server import build_server

    server = build_server(FakeReader(), sessionmaker(engine))
    tool_names = {t.name for t in server._tool_manager.list_tools()}
    assert {"list_my_namespaces", "list_secrets", "get_secret"} <= tool_names
