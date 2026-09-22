"""Tests for the MCP write tool logic (D12): create/update secret, request namespace.

Exercised with a fake reader + fake writer + in-memory datastore session. No
OpenBao, no running server. Dry-run is the default and must not touch the writer.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ladon.datastore import repositories as repo
from ladon.datastore.models import Base
from ladon.hygiene import REDACTED, PlaceholderSecretError
from ladon.mcp.ports import SecretPath, WriteResult
from ladon.mcp.server import (
    AccessDeniedError,
    create_secret_impl,
    request_namespace_impl,
    update_secret_impl,
)

USER = "op@example.com"


class FakeReader:
    def list_namespaces(self, user_email):
        return ["team-a"]

    def list_secrets(self, user_email, namespace):
        return [SecretPath("team-a/app", 1)]

    def get_secret(self, user_email, namespace, path, env):  # pragma: no cover - unused here
        raise NotImplementedError


class FakeWriter:
    def __init__(self):
        self.calls = []

    def create_secret(self, user_email, namespace, path, env, values):
        self.calls.append(("create", path, sorted(values)))
        return WriteResult(
            namespace=namespace, path=path, env=env, created=True, written_keys=list(values)
        )

    def update_secret(self, user_email, namespace, path, env, values, remove_keys):
        self.calls.append(("update", path, sorted(values), sorted(remove_keys)))
        return WriteResult(
            namespace=namespace,
            path=path,
            env=env,
            created=False,
            written_keys=list(values),
            removed_keys=list(remove_keys),
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


# ── create_secret ──────────────────────────────────────────────────────


def test_create_dry_run_does_not_write(session, allowed):
    writer = FakeWriter()
    result = create_secret_impl(
        FakeReader(),
        writer,
        session,
        user_email=USER,
        namespace="team-a",
        path="team-a/app",
        env="dev",
        values={"API_KEY": "realvalue123"},
    )
    assert result["applied"] is False
    assert result["action"] == "create"
    assert result["written_keys"] == ["API_KEY"]
    assert writer.calls == []  # nothing written on a dry-run
    # Audited as a dry-run, with the value redacted.
    call = repo.recent_calls(session)[0]
    assert call.status == "dry-run"
    assert call.redacted_args["data"] == REDACTED
    assert "realvalue123" not in str(call.redacted_args)


def test_create_apply_writes(session, allowed):
    writer = FakeWriter()
    result = create_secret_impl(
        FakeReader(),
        writer,
        session,
        user_email=USER,
        namespace="team-a",
        path="team-a/app",
        env="dev",
        values={"API_KEY": "realvalue123"},
        apply=True,
    )
    assert result["applied"] is True
    assert writer.calls == [("create", "team-a/app", ["API_KEY"])]


def test_create_refuses_placeholder_value(session, allowed):
    writer = FakeWriter()
    with pytest.raises(PlaceholderSecretError):
        create_secret_impl(
            FakeReader(),
            writer,
            session,
            user_email=USER,
            namespace="team-a",
            path="team-a/app",
            env="dev",
            values={"API_KEY": "changeme"},
            apply=True,
        )
    assert writer.calls == []  # refused before any write


def test_create_denied_off_allow_list(session):
    with pytest.raises(AccessDeniedError, match="allow list"):
        create_secret_impl(
            FakeReader(),
            FakeWriter(),
            session,
            user_email=USER,
            namespace="team-a",
            path="p",
            env="dev",
            values={"K": "v-real-1234"},
        )


def test_create_denied_foreign_namespace(session, allowed):
    with pytest.raises(AccessDeniedError, match="may not access"):
        create_secret_impl(
            FakeReader(),
            FakeWriter(),
            session,
            user_email=USER,
            namespace="team-z",
            path="p",
            env="dev",
            values={"K": "v-real-1234"},
        )


# ── update_secret ──────────────────────────────────────────────────────


def test_update_apply_merges_and_removes(session, allowed):
    writer = FakeWriter()
    result = update_secret_impl(
        FakeReader(),
        writer,
        session,
        user_email=USER,
        namespace="team-a",
        path="team-a/app",
        env="dev",
        values={"NEW": "brandnewval1"},
        remove_keys=["OLD"],
        apply=True,
    )
    assert result["applied"] is True
    assert result["written_keys"] == ["NEW"]
    assert result["removed_keys"] == ["OLD"]
    assert writer.calls == [("update", "team-a/app", ["NEW"], ["OLD"])]


def test_update_requires_some_change(session, allowed):
    with pytest.raises(ValueError, match="at least one"):
        update_secret_impl(
            FakeReader(),
            FakeWriter(),
            session,
            user_email=USER,
            namespace="team-a",
            path="team-a/app",
            env="dev",
        )


def test_update_dry_run_does_not_write(session, allowed):
    writer = FakeWriter()
    update_secret_impl(
        FakeReader(),
        writer,
        session,
        user_email=USER,
        namespace="team-a",
        path="team-a/app",
        env="dev",
        remove_keys=["OLD"],
    )
    assert writer.calls == []


# ── request_namespace ──────────────────────────────────────────────────


def test_request_namespace_records_row(session, allowed):
    result = request_namespace_impl(
        session,
        user_email=USER,
        team_name="team-new",
        environments=["dev"],
        members=[USER],
        description="new team",
    )
    assert result["status"] == "pending"
    assert result["team_name"] == "team-new"
    rows = repo.list_namespace_requests(session)
    assert len(rows) == 1 and rows[0].requester_email == USER


def test_request_namespace_requires_env(session, allowed):
    with pytest.raises(ValueError, match="at least one environment"):
        request_namespace_impl(session, user_email=USER, team_name="t", environments=[], members=[])


def test_request_namespace_denied_off_allow_list(session):
    with pytest.raises(AccessDeniedError, match="allow list"):
        request_namespace_impl(
            session, user_email=USER, team_name="t", environments=["dev"], members=[]
        )


def test_update_refuses_a_key_in_both_values_and_remove_keys(session, allowed):
    # Naming one key on both sides is ambiguous, so it is refused rather than
    # resolved by an ordering the caller cannot see.
    reader, writer = FakeReader(), FakeWriter()
    with pytest.raises(ValueError, match="both"):
        update_secret_impl(
            reader,
            writer,
            session,
            user_email=USER,
            namespace="team-a",
            path="team-a/app",
            env="dev",
            values={"API_KEY": "fresh-dummy-value"},
            remove_keys=["API_KEY"],
            apply=True,
        )
    assert writer.calls == []
