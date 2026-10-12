"""Denials and failures on the agent surface leave a row (D12, D14).

The tool functions audit what they can reach, but not the call they refuse
before they start (the allow list, the namespace gate), and not reliably any
failure at all: the row goes into the caller's session, and the exception
unwinds it before anything commits. The registered tools are wrapped in the
same recorder the HTTP routes use, so both surfaces leave the same rows.

Called through the registered tool, not the ``*_impl`` function, because the
wrapping is what is under test here.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sero.audit import DENIED, ERROR, REFUSED, UNIDENTIFIED
from sero.datastore import repositories as repo
from sero.datastore.models import Base
from sero.mcp.ports import SecretDetail, SecretPath, WriteResult
from sero.mcp.server import AccessDeniedError, build_server

USER = "op@example.com"
ADMIN = "admin@example.com"


class FakeReader:
    def list_namespaces(self, user_email):
        return ["team-a"]

    def list_secrets(self, user_email, namespace):
        return [SecretPath("team-a/app", 1)]

    def get_secret(self, user_email, namespace, path, env):
        return SecretDetail(
            namespace=namespace,
            path=path,
            env=env,
            exists=True,
            version=1,
            masked_keys={"API_KEY": "dum***"},
        )


class BrokenWriter:
    """A vault that takes the call and then will not write."""

    def create_secret(self, *a, **k):
        raise RuntimeError("the vault refused the write")

    def update_secret(self, *a, **k):  # pragma: no cover - not exercised
        raise RuntimeError("the vault refused the write")


class FineWriter:
    def create_secret(self, user_email, namespace, path, env, values):
        return WriteResult(
            namespace=namespace, path=path, env=env, created=True, written_keys=list(values)
        )

    def update_secret(self, *a, **k):  # pragma: no cover - not exercised
        raise NotImplementedError


class Ctx:
    """A request context carrying a bearer-validated identity, or none."""

    def __init__(self, email=None):
        self._email = email

    @property
    def request_context(self):
        if self._email is None:
            raise RuntimeError("no request context")
        token = type("T", (), {"user_email": self._email})()
        user = type("U", (), {"access_token": token})()
        return type("R", (), {"request": type("Q", (), {"user": user})()})()


@pytest.fixture
def sessions(tmp_path):
    # A file, not ":memory:": the recorder opens a second connection while the
    # tool's own is still open, and only a real file shows whether that works.
    engine = create_engine(f"sqlite:///{tmp_path / 'sero.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def tool(server, name):
    return server._tool_manager.get_tool(name).fn


def rows(sessions):
    with sessions() as session:
        return list(repo.recent_calls(session))


def test_a_denied_tool_call_leaves_a_denied_row(sessions):
    # Nobody is on the allow list, so the call is refused before the tool
    # function does anything, which is exactly the call worth recording.
    server = build_server(FakeReader(), sessions)

    with pytest.raises(AccessDeniedError):
        tool(server, "list_secrets")(namespace="team-a", ctx=Ctx(USER))

    (row,) = rows(sessions)
    assert (row.tool_name, row.status, row.user_email) == ("list_secrets", DENIED, USER)
    assert row.redacted_args == {"namespace": "team-a"}
    assert "allow list" in row.error


def test_a_call_with_no_identity_is_recorded_against_nobody(sessions):
    server = build_server(FakeReader(), sessions)

    with pytest.raises(AccessDeniedError):
        tool(server, "list_my_namespaces")(ctx=Ctx(None))

    (row,) = rows(sessions)
    assert (row.tool_name, row.status, row.user_email) == (
        "list_my_namespaces",
        DENIED,
        UNIDENTIFIED,
    )


def test_a_failing_writer_leaves_an_error_row(sessions):
    with sessions() as session:
        repo.allow(session, email=USER, added_by=ADMIN)
        session.commit()
    server = build_server(FakeReader(), sessions, writer=BrokenWriter())

    with pytest.raises(RuntimeError, match="refused the write"):
        tool(server, "create_secret")(
            namespace="team-a",
            path="app",
            env="dev",
            values={"API_KEY": "a-real-looking-value"},
            ctx=Ctx(USER),
            apply=True,
        )

    (row,) = rows(sessions)
    assert (row.tool_name, row.status) == ("create_secret", ERROR)
    # The arguments name the path, never a value.
    assert row.redacted_args["path"] == "app"
    assert "a-real-looking-value" not in str(row.redacted_args)


def test_a_call_for_another_environment_is_recorded_as_refused(sessions):
    with sessions() as session:
        repo.allow(session, email=USER, added_by=ADMIN)
        session.commit()
    server = build_server(FakeReader(), sessions, environment="dev")

    with pytest.raises(Exception, match="serves environment 'dev'"):
        tool(server, "get_secret")(namespace="team-a", path="app", env="prod", ctx=Ctx(USER))

    (row,) = rows(sessions)
    assert (row.tool_name, row.status) == ("get_secret", REFUSED)


def test_a_call_that_works_still_writes_exactly_one_row(sessions):
    # The wrapping records the unhappy endings; the tool function records the
    # happy one. A success must not end up with two rows.
    with sessions() as session:
        repo.allow(session, email=USER, added_by=ADMIN)
        session.commit()
    server = build_server(FakeReader(), sessions, writer=FineWriter())

    result = tool(server, "create_secret")(
        namespace="team-a",
        path="app",
        env="dev",
        values={"API_KEY": "a-real-looking-value"},
        ctx=Ctx(USER),
        apply=True,
    )

    assert result["applied"] is True
    (row,) = rows(sessions)
    assert (row.tool_name, row.status) == ("create_secret", "ok")


# The least each tool needs to be called at all, so the identity gate is what
# refuses it rather than Python.
MINIMAL_ARGS = {
    "list_my_namespaces": {},
    "list_secrets": {"namespace": "team-a"},
    "get_secret": {"namespace": "team-a", "path": "app", "env": "dev"},
    "request_namespace": {"team_name": "team-b", "environments": ["dev"]},
    "create_secret": {"namespace": "team-a", "path": "app", "env": "dev", "values": {}},
    "update_secret": {"namespace": "team-a", "path": "app", "env": "dev"},
}


def test_every_registered_tool_is_wrapped(sessions):
    # A tool added later without the wrapping is the failure this catches.
    server = build_server(FakeReader(), sessions, writer=FineWriter())
    names = [t.name for t in server._tool_manager.list_tools()]
    assert sorted(names) == sorted(MINIMAL_ARGS)

    for name in names:
        with pytest.raises(AccessDeniedError):
            tool(server, name)(ctx=Ctx(None), **MINIMAL_ARGS[name])

    recorded = {(row.tool_name, row.status, row.user_email) for row in rows(sessions)}
    assert recorded == {(name, DENIED, UNIDENTIFIED) for name in names}
