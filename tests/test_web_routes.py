"""Tests for the backend's HTTP routes (D14).

The contract is ``ui/src/lib/api.ts``: these tests call the paths, methods, query
parameters and JSON shapes the UI already uses. They run against a TestClient
with SQLite and an ``OpenBaoSecrets`` wired to a fake KV surface, so no OpenBao
and no browser are involved.

The fake stores plaintext, which is how a test can prove the masked routes never
return one.
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from fake_assertion import SETTINGS as ASSERTION_SETTINGS
from fake_assertion import token as assertion_token
from fake_assertion import verifier as assertion_verifier
from ladon.datastore import repositories as repo
from ladon.datastore.models import Base, ToolCallLog
from ladon.mcp.openbao import OpenBaoSecrets
from ladon.web.app import create_app
from ladon.web.groups import ConfigGroupSource
from ladon.web.settings import (
    DEFAULT_IDENTITY_HEADER,
    AccessSettings,
    IdentitySettings,
    WebSettings,
    parse_access,
)

MEMBER = "op@example.com"
ADMIN = "admin@example.com"
STRANGER = "stranger@example.com"
HEADER = DEFAULT_IDENTITY_HEADER

ACCESS = parse_access(
    {
        "admins": [ADMIN],
        "members": {MEMBER: ["team-a"]},
        "groups": {"platform": ["team-a"]},
        "group_members": {"platform": [ADMIN]},
    }
)


# ── Fakes ──────────────────────────────────────────────────────────────


class WriteBroke(RuntimeError):
    """What a vault that will not take a write looks like from up here."""


class FakeKvV2:
    """The ``client.secrets.kv.v2`` surface, over a plain dict of paths."""

    def __init__(self, store, *, writes_fail=False):
        self._store = store
        self._writes_fail = writes_fail

    def list_secrets(self, *, path, mount_point):
        return {"data": {"keys": list(self._store.keys())}}

    def read_secret_version(self, *, path, mount_point, raise_on_deleted_version=False):
        if path not in self._store:
            raise KeyError("not found")
        return {"data": {"data": dict(self._store[path]), "metadata": {"version": 3}}}

    def create_or_update_secret(self, *, path, secret, mount_point):
        if self._writes_fail:
            raise WriteBroke("the vault refused the write")
        self._store[path] = dict(secret)
        return {"data": {"version": 1}}


class FakeClient:
    def __init__(self, store, *, writes_fail=False):
        kv = FakeKvV2(store, writes_fail=writes_fail)
        self.secrets = type("S", (), {"kv": type("K", (), {"v2": kv})()})()


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def store():
    # One namespace's KV contents. Values are obvious dummies.
    return {"app": {"API_KEY": "dummy-api-key-value", "DB_URL": "dummy-db-url"}}


@pytest.fixture
def sessions(tmp_path):
    # A file, not ":memory:": the audit path opens a second connection while the
    # request's is still open, and only a real file shows whether that works.
    engine = create_engine(f"sqlite:///{tmp_path / 'ladon.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def allowed(sessions):
    """The allow list gates the tool functions, so seed everyone the tests use.

    The stranger is on it too: they are allowed to use Ladon and hold no
    namespace, which is what makes the namespace gate the thing under test
    rather than the allow list.
    """
    with sessions() as session:
        repo.allow(session, email=MEMBER, added_by=ADMIN)
        repo.allow(session, email=ADMIN, added_by=ADMIN)
        repo.allow(session, email=STRANGER, added_by=ADMIN)
        session.commit()


def build_settings(*, reveal_enabled=False, ui_dist=None, access=ACCESS, environment="dev"):
    return WebSettings(
        identity=IdentitySettings(),
        access=access,
        environment=environment,
        openbao_address="https://vault.example.com",
        kv_mount="kv",
        ui_dist=ui_dist,
        reveal_enabled=reveal_enabled,
    )


def build_client(sessions, store, *, writes_fail=False, raise_server_exceptions=True, **kwargs):
    settings = build_settings(**kwargs)
    groups = ConfigGroupSource(settings.access)
    client = FakeClient(store, writes_fail=writes_fail)
    secrets = OpenBaoSecrets(
        client_for=lambda namespace: client,
        namespaces_for=groups.namespaces_for,
    )
    app = create_app(settings, session_factory=sessions, secrets=secrets, groups=groups)
    # Starlette re-raises an unhandled exception after the 500 is sent, so a
    # test that wants to read that response has to ask it not to.
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


@pytest.fixture
def client(sessions, store, allowed):
    return build_client(sessions, store)


def as_member(client, method, path, **kwargs):
    return client.request(method, path, headers={HEADER: MEMBER}, **kwargs)


def audit_rows(sessions, tool_name):
    with sessions() as session:
        stmt = select(ToolCallLog).where(ToolCallLog.tool_name == tool_name)
        return list(session.scalars(stmt))


# ── Health and identity ────────────────────────────────────────────────


def test_healthz_needs_no_identity(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/me"),
        ("GET", "/api/namespaces"),
        ("GET", "/api/namespaces/team-a/secrets"),
        ("GET", "/api/namespaces/team-a/secrets/app?env=dev"),
        ("POST", "/api/namespaces/team-a/secrets"),
        ("PATCH", "/api/namespaces/team-a/secrets/app"),
        ("GET", "/api/namespaces/team-a/secrets/app/reveal?env=dev&key=API_KEY"),
        ("POST", "/api/namespace-requests"),
        ("GET", "/api/namespace-requests"),
        ("GET", "/api/admin/namespace-requests"),
        ("POST", "/api/admin/namespace-requests/1/approve"),
        ("POST", "/api/admin/namespace-requests/1/reject"),
        ("GET", "/api/admin/allow-list"),
        ("POST", "/api/admin/allow-list"),
        ("DELETE", f"/api/admin/allow-list/{MEMBER}"),
    ],
)
def test_every_api_route_refuses_a_request_with_no_identity(client, method, path):
    resp = client.request(method, path, json={})
    assert resp.status_code == 401
    assert resp.json() == {"error": "no identity"}


def test_me_reports_the_caller_their_namespaces_and_admin_standing(client):
    resp = as_member(client, "GET", "/api/me")
    assert resp.status_code == 200
    assert resp.json() == {
        "email": MEMBER,
        "namespaces": ["team-a"],
        "is_admin": False,
        "environment": "dev",
    }


def test_me_strips_the_issuer_prefix_the_proxy_adds(client):
    resp = client.get("/api/me", headers={HEADER: f"accounts.example.com:{MEMBER.upper()}"})
    assert resp.json()["email"] == MEMBER


def test_me_marks_an_admin(client):
    resp = client.get("/api/me", headers={HEADER: ADMIN})
    assert resp.json()["is_admin"] is True


# ── Namespaces and secrets ─────────────────────────────────────────────


def test_namespaces_lists_the_callers_namespaces(client):
    resp = as_member(client, "GET", "/api/namespaces")
    assert resp.json() == {"namespaces": ["team-a"]}


def test_secrets_listing_reports_paths_and_key_counts(client):
    resp = as_member(client, "GET", "/api/namespaces/team-a/secrets")
    assert resp.status_code == 200
    assert resp.json() == {"namespace": "team-a", "secrets": [{"path": "app", "key_count": 2}]}


def test_secret_detail_is_masked(client):
    resp = as_member(client, "GET", "/api/namespaces/team-a/secrets/app?env=dev")
    body = resp.json()
    assert body["exists"] is True
    assert body["version"] == 3
    assert set(body["masked_keys"]) == {"API_KEY", "DB_URL"}
    # A plaintext value never appears on this route.
    assert "dummy-api-key-value" not in resp.text
    assert body["masked_keys"]["API_KEY"].startswith("dum")
    assert body["masked_keys"]["API_KEY"].endswith("*")


def test_secret_detail_accepts_a_nested_path(client, store):
    store["apps/web/config"] = {"ONE": "dummy-one"}
    resp = as_member(client, "GET", "/api/namespaces/team-a/secrets/apps%2Fweb%2Fconfig?env=dev")
    assert resp.status_code == 200
    assert resp.json()["path"] == "apps/web/config"


def test_a_non_member_is_refused_the_namespace(client):
    resp = client.get("/api/namespaces/team-a/secrets", headers={HEADER: STRANGER})
    assert resp.status_code == 403
    assert "team-a" in resp.json()["error"]


def test_a_caller_off_the_allow_list_is_refused(sessions, store):
    # Nothing seeded: the allow list gates the agent and the HTTP surface alike.
    client = build_client(sessions, store)
    resp = as_member(client, "GET", "/api/namespaces")
    assert resp.status_code == 403


# ── Writes ─────────────────────────────────────────────────────────────


def test_create_defaults_to_a_dry_run(client, store, sessions):
    resp = as_member(
        client,
        "POST",
        "/api/namespaces/team-a/secrets",
        json={"path": "new", "env": "dev", "values": {"TOKEN": "dummy-new-value"}},
    )
    body = resp.json()
    assert body["applied"] is False
    assert body["action"] == "create"
    assert body["written_keys"] == ["TOKEN"]
    assert "new" not in store
    assert len(audit_rows(sessions, "create_secret")) == 1


def test_create_with_apply_writes_and_audits_without_the_value(client, store, sessions):
    resp = as_member(
        client,
        "POST",
        "/api/namespaces/team-a/secrets",
        json={
            "path": "new",
            "env": "dev",
            "values": {"TOKEN": "dummy-new-value"},
            "apply": True,
        },
    )
    assert resp.json()["applied"] is True
    assert store["new"] == {"TOKEN": "dummy-new-value"}
    (row,) = audit_rows(sessions, "create_secret")
    assert row.user_email == MEMBER
    assert "dummy-new-value" not in str(row.redacted_args)


def test_create_refuses_a_placeholder_value(client):
    resp = as_member(
        client,
        "POST",
        "/api/namespaces/team-a/secrets",
        json={"path": "new", "env": "dev", "values": {"TOKEN": "changeme"}, "apply": True},
    )
    assert resp.status_code == 400
    assert "TOKEN" in resp.json()["error"]


def test_update_merges_and_removes(client, store):
    resp = as_member(
        client,
        "PATCH",
        "/api/namespaces/team-a/secrets/app",
        json={
            "env": "dev",
            "values": {"API_KEY": "dummy-rotated-value"},
            "remove_keys": ["DB_URL"],
            "apply": True,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["action"] == "update"
    # API_KEY rotated, DB_URL gone, nothing else touched.
    assert store["app"] == {"API_KEY": "dummy-rotated-value"}


def test_update_refuses_a_key_named_on_both_sides(client, store):
    resp = as_member(
        client,
        "PATCH",
        "/api/namespaces/team-a/secrets/app",
        json={
            "env": "dev",
            "values": {"API_KEY": "dummy-rotated-value"},
            "remove_keys": ["API_KEY"],
            "apply": True,
        },
    )
    assert resp.status_code == 400
    assert "API_KEY" in resp.json()["error"]
    assert store["app"]["API_KEY"] == "dummy-api-key-value"


def test_update_refuses_to_empty_a_path(client, store):
    resp = as_member(
        client,
        "PATCH",
        "/api/namespaces/team-a/secrets/app",
        json={"env": "dev", "remove_keys": ["API_KEY", "DB_URL"], "apply": True},
    )
    assert resp.status_code == 400
    assert "empty" in resp.json()["error"]
    assert len(store["app"]) == 2


def test_update_needs_something_to_change(client):
    resp = as_member(
        client,
        "PATCH",
        "/api/namespaces/team-a/secrets/app",
        json={"env": "dev", "apply": True},
    )
    assert resp.status_code == 400


def test_writes_to_another_namespace_are_refused(client, store):
    resp = client.post(
        "/api/namespaces/team-a/secrets",
        headers={HEADER: STRANGER},
        json={"path": "new", "env": "dev", "values": {"TOKEN": "dummy"}, "apply": True},
    )
    assert resp.status_code == 403
    assert "new" not in store


# ── Reveal ─────────────────────────────────────────────────────────────


def test_reveal_is_refused_by_default(client):
    resp = as_member(client, "GET", "/api/namespaces/team-a/secrets/app/reveal?env=dev&key=API_KEY")
    assert resp.status_code == 403
    assert resp.json() == {"error": "reveal is not enabled in this deployment"}


def test_reveal_returns_the_value_when_the_deployment_enables_it(sessions, store, allowed):
    client = build_client(sessions, store, reveal_enabled=True)
    resp = as_member(client, "GET", "/api/namespaces/team-a/secrets/app/reveal?env=dev&key=API_KEY")
    assert resp.status_code == 200
    assert resp.json() == {"value": "dummy-api-key-value"}


def test_reveal_audits_the_key_name_only(sessions, store, allowed):
    client = build_client(sessions, store, reveal_enabled=True)
    as_member(client, "GET", "/api/namespaces/team-a/secrets/app/reveal?env=dev&key=API_KEY")
    (row,) = audit_rows(sessions, "reveal_value")
    assert row.user_email == MEMBER
    assert "dummy-api-key-value" not in str(row.redacted_args)


def test_reveal_is_refused_for_a_non_member(sessions, store, allowed):
    client = build_client(sessions, store, reveal_enabled=True)
    resp = client.get(
        "/api/namespaces/team-a/secrets/app/reveal?env=dev&key=API_KEY",
        headers={HEADER: STRANGER},
    )
    assert resp.status_code == 403


# ── Namespace requests ─────────────────────────────────────────────────


def test_request_namespace_records_a_pending_row(client):
    resp = as_member(
        client,
        "POST",
        "/api/namespace-requests",
        json={
            "team_name": "team-c",
            "environments": ["dev"],
            "members": [MEMBER],
            "description": "a new team",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["team_name"] == "team-c"
    assert isinstance(body["request_id"], int)


def test_a_caller_sees_only_their_own_requests(client, sessions):
    as_member(
        client,
        "POST",
        "/api/namespace-requests",
        json={"team_name": "team-c", "environments": ["dev"], "members": [MEMBER]},
    )
    with sessions() as session:
        repo.create_namespace_request(
            session,
            requester_email=ADMIN,
            team_name="team-d",
            environments=["dev"],
            members=[ADMIN],
        )
        session.commit()

    rows = as_member(client, "GET", "/api/namespace-requests").json()["requests"]
    assert [r["team_name"] for r in rows] == ["team-c"]
    assert rows[0]["requester_email"] == MEMBER
    assert rows[0]["created_at"]


# ── Admin ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/admin/namespace-requests"),
        ("POST", "/api/admin/namespace-requests/1/approve"),
        ("POST", "/api/admin/namespace-requests/1/reject"),
        ("GET", "/api/admin/allow-list"),
        ("POST", "/api/admin/allow-list"),
        ("DELETE", f"/api/admin/allow-list/{MEMBER}"),
    ],
)
def test_admin_routes_refuse_a_non_admin(client, method, path):
    resp = client.request(method, path, headers={HEADER: MEMBER}, json={"reason": "no"})
    assert resp.status_code == 403
    assert "admin" in resp.json()["error"]


def test_admin_lists_and_filters_requests(client, sessions):
    as_member(
        client,
        "POST",
        "/api/namespace-requests",
        json={"team_name": "team-c", "environments": ["dev"], "members": [MEMBER]},
    )
    listed = client.get("/api/admin/namespace-requests", headers={HEADER: ADMIN}).json()
    assert [r["team_name"] for r in listed["requests"]] == ["team-c"]

    filtered = client.get(
        "/api/admin/namespace-requests?status=approved", headers={HEADER: ADMIN}
    ).json()
    assert filtered["requests"] == []


def test_admin_approves_a_request(client, sessions):
    created = as_member(
        client,
        "POST",
        "/api/namespace-requests",
        json={"team_name": "team-c", "environments": ["dev"], "members": [MEMBER]},
    ).json()
    resp = client.post(
        f"/api/admin/namespace-requests/{created['request_id']}/approve",
        headers={HEADER: ADMIN},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert audit_rows(sessions, "approve_namespace_request")


def test_admin_rejects_a_request_with_a_reason(client):
    created = as_member(
        client,
        "POST",
        "/api/namespace-requests",
        json={"team_name": "team-c", "environments": ["dev"], "members": [MEMBER]},
    ).json()
    resp = client.post(
        f"/api/admin/namespace-requests/{created['request_id']}/reject",
        headers={HEADER: ADMIN},
        json={"reason": "use team-a"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


def test_deciding_an_unknown_request_is_a_404(client):
    resp = client.post("/api/admin/namespace-requests/404/approve", headers={HEADER: ADMIN})
    assert resp.status_code == 404


def test_admin_reads_adds_and_removes_allow_list_entries(client, sessions):
    seeded = {MEMBER, ADMIN, STRANGER}
    listed = client.get("/api/admin/allow-list", headers={HEADER: ADMIN}).json()
    assert {e["email"] for e in listed["entries"]} == seeded

    added = client.post(
        "/api/admin/allow-list",
        headers={HEADER: ADMIN},
        json={"email": "New@Example.com", "notes": "on call"},
    )
    assert added.status_code == 200
    assert added.json()["email"] == "new@example.com"
    assert added.json()["added_by"] == ADMIN

    removed = client.delete("/api/admin/allow-list/new@example.com", headers={HEADER: ADMIN})
    assert removed.status_code == 204
    after = client.get("/api/admin/allow-list", headers={HEADER: ADMIN}).json()
    assert {e["email"] for e in after["entries"]} == seeded
    assert audit_rows(sessions, "allow_list_add")
    assert audit_rows(sessions, "allow_list_remove")


def test_removing_an_absent_allow_list_entry_is_a_404(client):
    resp = client.delete("/api/admin/allow-list/nobody@example.com", headers={HEADER: ADMIN})
    assert resp.status_code == 404


# ── Serving the UI ─────────────────────────────────────────────────────


def test_without_a_bundle_the_root_is_a_plain_json_404(client):
    resp = client.get("/")
    assert resp.status_code == 404
    assert resp.json() == {"error": "not found"}


def test_an_unknown_api_path_is_a_json_404_not_the_app_shell(sessions, store, tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>shell</title>", encoding="utf-8")
    client = build_client(sessions, store, ui_dist=tmp_path)
    resp = client.get("/api/nope")
    assert resp.status_code == 404
    assert resp.json() == {"error": "not found"}


def test_the_bundle_is_served_with_an_app_shell_fallback(sessions, store, tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>shell</title>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("export const ok = true;\n", encoding="utf-8")
    client = build_client(sessions, store, ui_dist=tmp_path)

    assert "shell" in client.get("/").text
    assert client.get("/assets/app.js").status_code == 200
    # A client-side route the server knows nothing about still gets the shell.
    deep = client.get("/namespaces/team-a/secrets")
    assert deep.status_code == 200
    assert "shell" in deep.text


def test_the_bundle_cannot_be_escaped(sessions, store, tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>shell</title>", encoding="utf-8")
    secret_file = tmp_path.parent / "outside.txt"
    secret_file.write_text("not for the web", encoding="utf-8")
    client = build_client(sessions, store, ui_dist=tmp_path)

    # Percent-encoded, so the client cannot normalize it away before it is
    # sent: the containment check is what has to refuse this, not httpx.
    resp = client.get("/%2e%2e/outside.txt")
    assert "not for the web" not in resp.text
    assert "shell" in resp.text


# ── Unconfigured deployment ────────────────────────────────────────────


def test_secret_routes_say_so_when_openbao_is_not_configured(sessions):
    settings = WebSettings(
        identity=IdentitySettings(),
        access=AccessSettings(),
        environment="dev",
        openbao_address=None,
        kv_mount="kv",
        ui_dist=None,
        reveal_enabled=False,
    )
    client = TestClient(create_app(settings, session_factory=sessions))
    resp = client.get("/api/namespaces", headers={HEADER: MEMBER})
    assert resp.status_code == 503
    assert "OpenBao" in resp.json()["error"]


# ── Audit durability ───────────────────────────────────────────────────


def test_a_failed_write_still_leaves_an_audit_row(sessions, store, allowed):
    # The tool function's own row dies with the rolled-back session, so the
    # calls most worth investigating are exactly the ones that would vanish.
    client = build_client(sessions, store, writes_fail=True, raise_server_exceptions=False)
    resp = as_member(
        client,
        "POST",
        "/api/namespaces/team-a/secrets",
        json={
            "path": "new",
            "env": "dev",
            "values": {"TOKEN": "dummy-new-value"},
            "apply": True,
        },
    )
    assert resp.status_code == 500
    assert resp.json() == {"error": "internal error"}

    (row,) = audit_rows(sessions, "create_secret")
    assert row.status == "error"
    assert row.user_email == MEMBER
    assert row.redacted_args["path"] == "new"
    assert "dummy-new-value" not in str(row.redacted_args) + str(row.error)


def test_an_unexpected_failure_says_nothing_about_itself(sessions, store, allowed):
    client = build_client(sessions, store, writes_fail=True, raise_server_exceptions=False)
    resp = as_member(
        client,
        "POST",
        "/api/namespaces/team-a/secrets",
        json={"path": "new", "env": "dev", "values": {"TOKEN": "dummy"}, "apply": True},
    )
    # The caller learns nothing about the internals; the traceback is in the log.
    assert resp.json() == {"error": "internal error"}
    assert "vault refused" not in resp.text


def test_a_reveal_the_gate_refuses_is_audited_as_denied(client, sessions):
    as_member(client, "GET", "/api/namespaces/team-a/secrets/app/reveal?env=dev&key=API_KEY")
    (row,) = audit_rows(sessions, "reveal_value")
    assert row.status == "denied"
    assert row.user_email == MEMBER
    assert "not enabled" in row.error


def test_a_reveal_denied_by_namespace_is_audited_with_the_reason(sessions, store, allowed):
    client = build_client(sessions, store, reveal_enabled=True)
    client.get(
        "/api/namespaces/team-a/secrets/app/reveal?env=dev&key=API_KEY",
        headers={HEADER: STRANGER},
    )
    (row,) = audit_rows(sessions, "reveal_value")
    assert row.status == "denied"
    assert row.user_email == STRANGER
    assert "namespace" in row.error


def test_a_denied_write_is_audited_as_denied(client, sessions):
    client.post(
        "/api/namespaces/team-a/secrets",
        headers={HEADER: STRANGER},
        json={"path": "new", "env": "dev", "values": {"TOKEN": "dummy"}, "apply": True},
    )
    (row,) = audit_rows(sessions, "create_secret")
    assert row.status == "denied"
    assert row.user_email == STRANGER


# ── Refusals, previewed and named ──────────────────────────────────────


def test_a_dry_run_previews_the_refusal_to_empty_a_path(client, store):
    resp = as_member(
        client,
        "PATCH",
        "/api/namespaces/team-a/secrets/app",
        json={"env": "dev", "remove_keys": ["API_KEY", "DB_URL"]},
    )
    assert resp.status_code == 400
    assert "empty" in resp.json()["error"]
    assert len(store["app"]) == 2


def test_creating_a_path_ending_in_reveal_is_refused(client, store):
    resp = as_member(
        client,
        "POST",
        "/api/namespaces/team-a/secrets",
        json={
            "path": "app/reveal",
            "env": "dev",
            "values": {"TOKEN": "dummy-token-value"},
            "apply": True,
        },
    )
    assert resp.status_code == 400
    assert "reserved" in resp.json()["error"]
    assert "app/reveal" not in store


def test_an_unexpected_value_error_is_not_reported_as_a_refusal(sessions, store, allowed):
    # Only a deliberate refusal is a 400. A ValueError from anywhere else is a
    # bug, and a bug is a 500 that says nothing.
    class Exploding:
        def list_namespaces(self, user_email):
            raise ValueError("a sentinel from somewhere unexpected")

    settings = build_settings()
    app = create_app(settings, session_factory=sessions, secrets=Exploding())
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/namespaces", headers={HEADER: MEMBER})
    assert resp.status_code == 500
    assert resp.json() == {"error": "internal error"}
    assert "sentinel" not in resp.text


# ── One deployment, one environment ────────────────────────────────────


def test_reading_another_environment_is_refused(client):
    resp = as_member(client, "GET", "/api/namespaces/team-a/secrets/app?env=prod")
    assert resp.status_code == 400
    assert resp.json() == {"error": "this deployment serves environment 'dev'"}


def test_creating_in_another_environment_is_refused(client, store):
    resp = as_member(
        client,
        "POST",
        "/api/namespaces/team-a/secrets",
        json={
            "path": "new",
            "env": "prod",
            "values": {"TOKEN": "dummy-token-value"},
            "apply": True,
        },
    )
    assert resp.status_code == 400
    assert "environment 'dev'" in resp.json()["error"]
    assert "new" not in store


def test_updating_in_another_environment_is_refused(client, store):
    resp = as_member(
        client,
        "PATCH",
        "/api/namespaces/team-a/secrets/app",
        json={"env": "prod", "values": {"API_KEY": "dummy-rotated"}, "apply": True},
    )
    assert resp.status_code == 400
    assert store["app"]["API_KEY"] == "dummy-api-key-value"


def test_revealing_from_another_environment_is_refused(sessions, store, allowed):
    client = build_client(sessions, store, reveal_enabled=True)
    resp = as_member(
        client, "GET", "/api/namespaces/team-a/secrets/app/reveal?env=prod&key=API_KEY"
    )
    assert resp.status_code == 400
    assert "dummy-api-key-value" not in resp.text


def test_an_undeclared_environment_pins_nothing(sessions, store, allowed):
    # An instance that has not said which environment it serves still works;
    # it just cannot enforce something nobody told it.
    client = build_client(sessions, store, environment=None)
    assert (
        as_member(client, "GET", "/api/namespaces/team-a/secrets/app?env=prod").status_code == 200
    )
    assert as_member(client, "GET", "/api/me").json()["environment"] is None


# ── The proxy's assertion ──────────────────────────────────────────────


def test_two_identity_headers_are_refused(client):
    # Duplicated headers mean something on the path is rewriting them, and
    # picking one is how a proxy's value gets replaced by an attacker's.
    resp = client.get("/api/me", headers=[(HEADER, MEMBER), (HEADER, "attacker@example.com")])
    assert resp.status_code == 401
    assert resp.json() == {"error": "no identity"}


def test_a_configured_assertion_is_required_and_decides(sessions, store, allowed):
    settings = build_settings()
    groups = ConfigGroupSource(settings.access)
    secrets = OpenBaoSecrets(
        client_for=lambda namespace: FakeClient(store),
        namespaces_for=groups.namespaces_for,
    )
    app = create_app(
        settings,
        session_factory=sessions,
        secrets=secrets,
        groups=groups,
        assertion_verifier=assertion_verifier(),
    )
    client = TestClient(app)

    # No assertion: refused, even with the plain header set.
    assert client.get("/api/me", headers={HEADER: MEMBER}).status_code == 401

    # A valid assertion decides, and the plain header is ignored.
    resp = client.get(
        "/api/me",
        headers={
            ASSERTION_SETTINGS.header: assertion_token(email=MEMBER),
            HEADER: "attacker@example.com",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["email"] == MEMBER

    # A forged one is no identity at all.
    forged = assertion_token(email=MEMBER, issuer="https://impostor.example.com")
    assert client.get("/api/me", headers={ASSERTION_SETTINGS.header: forged}).status_code == 401


def test_a_malformed_body_is_refused_without_echoing_it(client, store):
    # A value of the wrong type could be anything, including a secret typed
    # into the wrong field, so the refusal must not quote what was sent.
    resp = as_member(
        client,
        "POST",
        "/api/namespaces/team-a/secrets",
        json={
            "path": "new",
            "env": "dev",
            "values": {"TOKEN": {"nested": "sentinel-not-a-string"}},
            "apply": True,
        },
    )
    assert resp.status_code == 422
    assert "sentinel-not-a-string" not in resp.text
    assert "values" in resp.json()["error"]
    assert "new" not in store


def test_a_real_route_asked_with_the_wrong_method_is_a_json_405(client):
    resp = as_member(client, "DELETE", "/api/namespaces/team-a/secrets")
    assert resp.status_code == 405
    assert "error" in resp.json()
