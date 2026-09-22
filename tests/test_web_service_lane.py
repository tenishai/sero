"""Tests for the machine lane (D16).

A machine caller presents an OIDC ID token, not a proxy header: these run
against a TestClient whose service callers are pinned to a key pair generated
in-process by ``tests/fake_assertion.py``. Nothing here is a real credential;
the tokens verify nowhere but in this test run.

The acting email deliberately holds no namespace of its own, so every write that
lands proves the lane authorized on the caller's pinned namespaces rather than
on the human it names.
"""

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from fake_assertion import AUDIENCE as HUMAN_AUDIENCE
from fake_assertion import ISSUER, JWKS_URL, KEY, KID, OTHER_KEY, jwks, token
from fake_cluster import FakeCluster
from ladon.config.teams import ConfigError
from ladon.datastore.models import Base, ToolCallLog
from ladon.install.k8s import converge_documents
from ladon.mcp.openbao import OpenBaoSecrets
from ladon.web.app import create_app
from ladon.web.service_lane import ClusterSync, build_service_lane
from ladon.web.settings import (
    DEFAULT_IDENTITY_HEADER,
    AccessSettings,
    IdentitySettings,
    WebSettings,
    parse_service_callers,
)

CALLER = "assembly-room"
SUBJECT = "system:serviceaccount:ladon:assembly-room"
SERVICE_AUDIENCE = "https://ladon.example.com/service"
ACTING = "op@example.com"
NAMESPACE = "team-a"
OTHER_NAMESPACE = "team-b"
APP = "sample"
PATH = f"apps/{APP}/demo"

CALLER_CONFIG = {
    "name": CALLER,
    "issuer": ISSUER,
    "jwks_url": JWKS_URL,
    "audience": SERVICE_AUDIENCE,
    "subject": SUBJECT,
    "namespaces": [NAMESPACE],
    "acting_email_required": True,
}


def callers(**overrides):
    return parse_service_callers([{**CALLER_CONFIG, **overrides}], human_audience=HUMAN_AUDIENCE)


def service_token(*, claims=None, **kwargs):
    """A machine-lane token: the service audience, a subject, and no email."""
    payload = {"sub": SUBJECT, **(claims or {})}
    kwargs.setdefault("audience", SERVICE_AUDIENCE)
    kwargs.setdefault("issuer", ISSUER)
    return token(email=None, claims=payload, **kwargs)


def bearer(value=None):
    return {"authorization": f"Bearer {value if value is not None else service_token()}"}


def put(client, body=None, *, token_value=None, headers=None):
    """POST one machine-lane write, with a valid token unless told otherwise."""
    return client.post(
        "/api/service/secrets",
        json=put_body() if body is None else body,
        headers=bearer(token_value) if headers is None else headers,
    )


def sync(client, body=None, *, headers=None):
    return client.post(
        "/api/service/sync",
        json=sync_body() if body is None else body,
        headers=bearer() if headers is None else headers,
    )


# ── Fakes ──────────────────────────────────────────────────────────────


class FakeKvV2:
    """The ``client.secrets.kv.v2`` surface, over a plain dict of paths."""

    def __init__(self, store):
        self._store = store

    def list_secrets(self, *, path, mount_point):
        return {"data": {"keys": list(self._store.keys())}}

    def read_secret_version(self, *, path, mount_point, raise_on_deleted_version=False):
        if path not in self._store:
            raise KeyError("not found")
        return {"data": {"data": dict(self._store[path]), "metadata": {"version": 2}}}

    def create_or_update_secret(self, *, path, secret, mount_point):
        self._store[path] = dict(secret)
        return {"data": {"version": 1}}


class FakeClient:
    def __init__(self, store):
        kv = FakeKvV2(store)
        self.secrets = type("S", (), {"kv": type("K", (), {"v2": kv})()})()


class FakeClock:
    """A clock a test moves by hand, for the rate limit's refill."""

    def __init__(self):
        self.moment = 0.0

    def __call__(self):
        return self.moment


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def store():
    # One namespace's KV contents. Values are obvious dummies.
    return {PATH: {"API_KEY": "dummy-api-key-value", "DB_URL": "dummy-db-url"}}


@pytest.fixture
def sessions(tmp_path):
    # A file, not ":memory:": the audit path opens a second connection while the
    # request's is still open, and only a real file shows whether that works.
    engine = create_engine(f"sqlite:///{tmp_path / 'ladon.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def cluster():
    return FakeCluster()


def build_client(sessions, store, cluster, *, service_callers, clock=None, identity=None, env=None):
    settings = WebSettings(
        identity=identity or IdentitySettings(),
        access=AccessSettings(),
        environment="dev",
        openbao_address=None,
        kv_mount="kv",
        ui_dist=None,
        reveal_enabled=False,
        service_callers=service_callers,
    )
    secrets = OpenBaoSecrets(
        client_for=lambda namespace: FakeClient(store),
        # The acting human holds nothing. A write that lands proves the lane
        # authorized on the caller's pinned namespaces and not on this.
        namespaces_for=lambda email: [],
        mount="kv",
    )
    lane = build_service_lane(
        service_callers,
        fetch=lambda url: jwks((KEY, KID)),
        cluster=ClusterSync(
            api_version=lambda: "v1",
            converge=lambda docs, **kwargs: converge_documents(docs, run=cluster, **kwargs),
        ),
        now=clock or (lambda: 0.0),
    )
    app = create_app(
        settings,
        session_factory=sessions,
        secrets=secrets,
        service_lane=lane,
        env=env or {},
    )
    return TestClient(app)


@pytest.fixture
def client(sessions, store, cluster):
    return build_client(sessions, store, cluster, service_callers=callers())


def put_body(**overrides):
    body = {
        "acting_email": ACTING,
        "namespace": NAMESPACE,
        "path": PATH,
        "values": {"DB_URL": "dummy-new-db-url"},
        "apply": False,
    }
    body.update(overrides)
    return body


def sync_body(**overrides):
    body = {
        "namespace": NAMESPACE,
        "app": APP,
        "path": PATH,
        "k8s_namespace": "ladon-sample",
        "name": "sample-demo",
        "apply": False,
    }
    body.update(overrides)
    return body


def rows(sessions, tool_name):
    with sessions() as session:
        return list(
            session.scalars(select(ToolCallLog).where(ToolCallLog.tool_name == tool_name)).all()
        )


# ── Configuration ──────────────────────────────────────────────────────


def test_a_service_audience_equal_to_the_human_audience_is_refused():
    # A human-lane token would otherwise be replayable on the machine lane.
    with pytest.raises(ConfigError, match="audience"):
        callers(audience=HUMAN_AUDIENCE)


def test_a_plain_http_key_url_is_refused():
    with pytest.raises(ConfigError, match="https"):
        callers(jwks_url="http://keys.example.com/keys")


def test_a_caller_with_no_subject_is_refused():
    with pytest.raises(ConfigError, match="subject"):
        callers(subject="")


def test_a_caller_with_no_namespaces_is_refused():
    with pytest.raises(ConfigError, match="namespaces"):
        callers(namespaces=[])


def test_two_callers_sharing_an_issuer_and_audience_are_refused():
    with pytest.raises(ConfigError, match="issuer"):
        parse_service_callers(
            [CALLER_CONFIG, {**CALLER_CONFIG, "name": "other"}],
            human_audience=HUMAN_AUDIENCE,
        )


def test_a_caller_that_would_not_record_who_it_acts_for_is_refused():
    with pytest.raises(ConfigError, match="acting_email_required"):
        callers(acting_email_required=False)


# ── Authentication ─────────────────────────────────────────────────────


def test_a_valid_token_gets_a_dry_run_report(client):
    response = client.post("/api/service/secrets", json=put_body(), headers=bearer())
    assert response.status_code == 200
    assert response.json() == {
        "applied": False,
        "action": "update",
        "namespace": NAMESPACE,
        "path": PATH,
        "env": "dev",
        "written_keys": ["DB_URL"],
        "removed_keys": [],
    }


def test_no_token_is_refused(client):
    assert client.post("/api/service/secrets", json=put_body()).status_code == 401


def test_a_token_for_the_human_lane_is_refused(client):
    replayed = service_token(audience=HUMAN_AUDIENCE)
    response = client.post("/api/service/secrets", json=put_body(), headers=bearer(replayed))
    assert response.status_code == 401


def test_a_token_for_another_audience_is_refused(client):
    stray = service_token(audience="https://ladon.example.com/other")
    assert put(client, token_value=stray).status_code == 401


def test_a_token_from_another_issuer_is_refused(client):
    stray = service_token(issuer="https://impostor.example.com")
    assert put(client, token_value=stray).status_code == 401


def test_a_token_naming_another_subject_is_refused(client):
    stray = service_token(claims={"sub": "system:serviceaccount:ladon:something-else"})
    assert put(client, token_value=stray).status_code == 401


def test_a_token_with_no_subject_is_refused(client):
    stray = token(email=None, audience=SERVICE_AUDIENCE, issuer=ISSUER)
    assert put(client, token_value=stray).status_code == 401


def test_an_expired_token_is_refused(client):
    stale = service_token(expires_in=timedelta(minutes=-5))
    assert put(client, token_value=stale).status_code == 401


def test_a_token_signed_by_another_key_is_refused(client):
    forged = service_token(key=OTHER_KEY)
    assert put(client, token_value=forged).status_code == 401


def test_a_token_that_is_not_a_bearer_credential_is_refused(client):
    headers = {"authorization": f"Basic {service_token()}"}
    assert client.post("/api/service/secrets", json=put_body(), headers=headers).status_code == 401


def test_the_identity_header_is_not_a_way_into_the_machine_lane(client):
    headers = {DEFAULT_IDENTITY_HEADER: ACTING}
    assert client.post("/api/service/secrets", json=put_body(), headers=headers).status_code == 401


def test_the_development_hatch_is_not_a_way_into_the_machine_lane(sessions, store, cluster):
    hatched = build_client(
        sessions,
        store,
        cluster,
        service_callers=callers(),
        identity=IdentitySettings(allow_dev_identity=True),
        env={"LADON_DEV_IDENTITY": ACTING},
    )
    assert hatched.post("/api/service/secrets", json=put_body()).status_code == 401


def test_a_deployment_with_no_service_callers_refuses_the_lane(sessions, store, cluster):
    closed = build_client(sessions, store, cluster, service_callers=())
    assert closed.post("/api/service/secrets", json=put_body(), headers=bearer()).status_code == 401


# ── Authorization and the path rule ────────────────────────────────────


def test_a_namespace_the_caller_is_not_pinned_to_is_refused(client, store):
    response = client.post(
        "/api/service/secrets",
        json=put_body(namespace=OTHER_NAMESPACE, apply=True),
        headers=bearer(),
    )
    assert response.status_code == 403
    assert store[PATH]["DB_URL"] == "dummy-db-url"


def test_a_path_outside_the_applications_prefix_is_refused(client):
    response = client.post(
        "/api/service/secrets", json=put_body(path="platform/demo"), headers=bearer()
    )
    assert response.status_code == 400


def test_a_path_that_climbs_out_of_the_prefix_is_refused(client):
    response = client.post(
        "/api/service/secrets", json=put_body(path="apps/../platform/demo"), headers=bearer()
    )
    assert response.status_code == 400


def test_a_missing_acting_email_is_refused(client):
    body = put_body()
    del body["acting_email"]
    assert client.post("/api/service/secrets", json=body, headers=bearer()).status_code == 422


def test_an_acting_email_that_is_not_an_address_is_refused(client):
    body = put_body(acting_email="not-an-address")
    assert client.post("/api/service/secrets", json=body, headers=bearer()).status_code == 422


# ── The merge rules ────────────────────────────────────────────────────


def test_an_applied_write_merges_and_leaves_the_other_keys_alone(client, store):
    response = client.post("/api/service/secrets", json=put_body(apply=True), headers=bearer())
    assert response.status_code == 200
    assert response.json()["applied"] is True
    assert store[PATH] == {"API_KEY": "dummy-api-key-value", "DB_URL": "dummy-new-db-url"}


def test_a_new_path_is_created(client, store):
    fresh = f"apps/{APP}/fresh"
    response = client.post(
        "/api/service/secrets",
        json=put_body(path=fresh, values={"TOKEN_URL": "https://example.com/token"}, apply=True),
        headers=bearer(),
    )
    assert response.status_code == 200
    assert response.json()["action"] == "create"
    assert sorted(store[fresh]) == ["TOKEN_URL"]


def test_remove_keys_removes(client, store):
    response = client.post(
        "/api/service/secrets",
        json=put_body(values={}, remove_keys=["DB_URL"], apply=True),
        headers=bearer(),
    )
    assert response.status_code == 200
    assert response.json()["removed_keys"] == ["DB_URL"]
    assert store[PATH] == {"API_KEY": "dummy-api-key-value"}


def test_a_key_named_in_both_values_and_remove_keys_is_refused(client, store):
    response = client.post(
        "/api/service/secrets",
        json=put_body(remove_keys=["DB_URL"], apply=True),
        headers=bearer(),
    )
    assert response.status_code == 400
    assert store[PATH]["DB_URL"] == "dummy-db-url"


def test_a_merge_that_would_empty_the_path_is_refused(client, store):
    response = client.post(
        "/api/service/secrets",
        json=put_body(values={}, remove_keys=["API_KEY", "DB_URL"], apply=True),
        headers=bearer(),
    )
    assert response.status_code == 400
    assert sorted(store[PATH]) == ["API_KEY", "DB_URL"]


def test_a_placeholder_value_is_refused(client, store):
    response = client.post(
        "/api/service/secrets",
        json=put_body(values={"DB_URL": "changeme"}, apply=True),
        headers=bearer(),
    )
    assert response.status_code == 400
    assert store[PATH]["DB_URL"] == "dummy-db-url"


def test_a_write_with_neither_values_nor_remove_keys_is_refused(client):
    response = client.post(
        "/api/service/secrets", json=put_body(values={}, apply=True), headers=bearer()
    )
    assert response.status_code == 400


def test_the_report_never_carries_a_value(client):
    response = client.post("/api/service/secrets", json=put_body(apply=True), headers=bearer())
    assert "dummy-new-db-url" not in response.text
    assert "dummy-api-key-value" not in response.text


# ── Sync ───────────────────────────────────────────────────────────────


def test_sync_converges_one_external_secret(client, cluster):
    dry = client.post("/api/service/sync", json=sync_body(), headers=bearer())
    assert dry.status_code == 200
    assert dry.json()["applied"] is False
    assert dry.json()["report"] == ["ExternalSecret/ladon-sample/sample-demo: create"]

    applied = client.post("/api/service/sync", json=sync_body(apply=True), headers=bearer())
    assert applied.status_code == 200
    assert applied.json()["report"] == ["ExternalSecret/ladon-sample/sample-demo: create"]

    again = client.post("/api/service/sync", json=sync_body(apply=True), headers=bearer())
    assert again.json()["report"] == ["ExternalSecret/ladon-sample/sample-demo: already current"]
    # Three calls, one write: the dry run asked the server and the second apply
    # found the document already current, so neither sent anything.
    written = [call for call in cluster.applies if "--dry-run=server" not in call]
    assert len(written) == 1


def test_sync_points_at_the_applications_own_store(client, cluster):
    client.post("/api/service/sync", json=sync_body(apply=True), headers=bearer())
    document = cluster.streams[0]
    assert "kind: ExternalSecret" in document
    assert f"key: {PATH}" in document
    assert "ladon-team-a-sample-" in document


def test_sync_targets_the_secret_name_the_consumer_already_uses(client, cluster):
    client.post(
        "/api/service/sync",
        json=sync_body(target_name="sample-credentials", apply=True),
        headers=bearer(),
    )
    assert "name: sample-credentials" in cluster.streams[0]


def test_sync_refuses_a_path_outside_the_applications_prefix(client, cluster):
    response = client.post(
        "/api/service/sync", json=sync_body(path="apps/other/demo"), headers=bearer()
    )
    assert response.status_code == 400
    assert cluster.calls == []


def test_sync_refuses_a_namespace_the_caller_is_not_pinned_to(client, cluster):
    response = client.post(
        "/api/service/sync", json=sync_body(namespace=OTHER_NAMESPACE), headers=bearer()
    )
    assert response.status_code == 403
    assert cluster.calls == []


def test_sync_needs_a_service_token(client):
    assert client.post("/api/service/sync", json=sync_body()).status_code == 401


# ── The rate limit ─────────────────────────────────────────────────────


def test_the_rate_limit_answers_429(sessions, store, cluster):
    clock = FakeClock()
    limited = build_client(
        sessions, store, cluster, service_callers=callers(rate_limit_per_minute=2), clock=clock
    )
    for _ in range(2):
        assert put(limited).status_code == 200
    assert put(limited).status_code == 429
    # Both routes draw on the same bucket: one caller, one budget.
    assert sync(limited).status_code == 429

    clock.moment += 60
    assert put(limited).status_code == 200


# ── Auditing ───────────────────────────────────────────────────────────


def test_a_successful_write_is_audited_against_the_human_and_the_caller(client, sessions):
    client.post("/api/service/secrets", json=put_body(apply=True), headers=bearer())
    (row,) = rows(sessions, "service.put_secret")
    assert row.user_email == ACTING
    assert row.status == "ok"
    assert row.redacted_args["caller"] == CALLER
    assert row.redacted_args["subject"] == SUBJECT
    assert row.redacted_args["path"] == PATH
    # The values went to OpenBao, never to the audit table.
    assert row.redacted_args["data"] == "<redacted>"


def test_a_dry_run_is_audited_as_one(client, sessions):
    client.post("/api/service/secrets", json=put_body(), headers=bearer())
    (row,) = rows(sessions, "service.put_secret")
    assert row.status == "dry-run"


def test_a_refused_token_is_audited(client, sessions):
    put(client, token_value=service_token(key=OTHER_KEY))
    (row,) = rows(sessions, "service.put_secret")
    assert row.status == "denied"
    assert row.redacted_args["caller"] == CALLER


def test_a_refused_namespace_is_audited(client, sessions):
    client.post("/api/service/secrets", json=put_body(namespace=OTHER_NAMESPACE), headers=bearer())
    (row,) = rows(sessions, "service.put_secret")
    assert row.status == "denied"
    assert row.redacted_args["namespace"] == OTHER_NAMESPACE
    assert row.redacted_args["caller"] == CALLER


def test_a_sync_is_audited(client, sessions):
    client.post("/api/service/sync", json=sync_body(apply=True), headers=bearer())
    (row,) = rows(sessions, "service.sync_secret")
    assert row.status == "ok"
    assert row.redacted_args["caller"] == CALLER
    assert row.redacted_args["k8s_namespace"] == "ladon-sample"
