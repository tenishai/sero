"""Tests for the machine lane (D16).

A machine caller presents an OIDC ID token, not a proxy header: these run
against a TestClient whose service callers are pinned to a key pair generated
in-process by ``tests/fake_assertion.py``. Nothing here is a real credential;
the tokens verify nowhere but in this test run.

The acting email deliberately holds no namespace of its own, so every write that
lands proves the lane authorized on the caller's pinned namespaces rather than
on the human it names.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from fake_assertion import AUDIENCE as HUMAN_AUDIENCE
from fake_assertion import ISSUER, JWKS_URL, KEY, KID, OTHER_KEY, jwks, token
from fake_assertion import SETTINGS as ASSERTION_SETTINGS
from fake_assertion import token as assertion_token
from fake_assertion import verifier as assertion_verifier
from fake_cluster import FakeCluster, eso_status_entry, managed_field_entry
from ladon.config.apps import MAX_KV_PATH_LENGTH, app_store_name
from ladon.config.teams import ConfigError
from ladon.datastore.models import Base, ToolCallLog
from ladon.hygiene import mask
from ladon.install.k8s import converge_documents, delete_documents, read_one
from ladon.mcp.openbao import OpenBaoSecrets
from ladon.web.app import create_app
from ladon.web.assertion import STALE_KEYS_MAX_SECONDS
from ladon.web.service_lane import (
    ACTING_EMAIL_MAX,
    MAX_SERVICE_TOKEN_AGE_SECONDS,
    SERVICE_STALE_KEYS_MAX_SECONDS,
    ClusterSync,
    build_service_lane,
)
from ladon.web.settings import (
    DEFAULT_IDENTITY_HEADER,
    DEFAULT_SERVICE_RATE_LIMIT,
    DEFAULT_SERVICE_READ_LIMIT,
    AccessSettings,
    IdentitySettings,
    WebSettings,
    parse_service_callers,
)


def now_epoch():
    return datetime.now(UTC).timestamp()


# What the store says about when the current version of PATH was written.
WRITTEN_AT = "2026-09-22T10:00:00.000000Z"

CALLER = "provisioner"
SUBJECT = "system:serviceaccount:ladon:provisioner"
SERVICE_AUDIENCE = "https://ladon.example.com/service"
ACTING = "op@example.com"
NAMESPACE = "team-a"
NESTED_NAMESPACE = "platform/team-a"
OTHER_NAMESPACE = "team-b"
APP = "sample"
PATH = f"apps/{APP}/demo"
# Where an ExternalSecret lands unless the caller is pinned to somewhere else:
# the application's own name, which is what onboarding defaults to.
DEFAULT_K8S_NAMESPACE = APP
PINNED_K8S_NAMESPACE = "ladon-sample"

CALLER_CONFIG = {
    "name": CALLER,
    "issuer": ISSUER,
    "jwks_url": JWKS_URL,
    "audience": SERVICE_AUDIENCE,
    "subject": SUBJECT,
    "namespaces": [NAMESPACE],
    # Empty on purpose: the common caller may reach the default Kubernetes
    # namespace and nowhere else.
    "k8s_namespaces": [],
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


def unsync(client, body=None, *, headers=None):
    """DELETE one machine-lane ExternalSecret."""
    return client.request(
        "DELETE",
        "/api/service/sync",
        json=unsync_body() if body is None else body,
        headers=bearer() if headers is None else headers,
    )


def erase(client, body=None, *, headers=None):
    """DELETE one machine-lane secret. A body on a DELETE, like the sync one."""
    return client.request(
        "DELETE",
        "/api/service/secrets",
        json=erase_body() if body is None else body,
        headers=bearer() if headers is None else headers,
    )


def read(client, *, headers=None, **overrides):
    """GET one path's key names. Arguments travel in the query string."""
    params = {"acting_email": ACTING, "namespace": NAMESPACE, "path": PATH, **overrides}
    return client.get(
        "/api/service/secrets",
        params={k: v for k, v in params.items() if v is not None},
        headers=bearer() if headers is None else headers,
    )


# ── Fakes ──────────────────────────────────────────────────────────────


class InvalidPath(Exception):
    """What hvac raises for a 404. The only failure that means "not there"."""


class Forbidden(Exception):
    """A denial from OpenBao, shaped like hvac's, with its status code."""

    status_code = 403


class FakeKvV2:
    """The ``client.secrets.kv.v2`` surface, over a plain dict of paths."""

    def __init__(self, store):
        self._store = store

    def list_secrets(self, *, path, mount_point):
        return {"data": {"keys": list(self._store.keys())}}

    def read_secret_version(self, *, path, mount_point, raise_on_deleted_version=False):
        if path not in self._store:
            raise InvalidPath(f"no secret at {path!r}")
        return {
            "data": {
                "data": dict(self._store[path]),
                "metadata": {"version": 2, "created_time": WRITTEN_AT},
            }
        }

    def create_or_update_secret(self, *, path, secret, mount_point):
        self._store[path] = dict(secret)
        return {"data": {"version": 1}}

    def delete_metadata_and_all_versions(self, *, path, mount_point):
        self._store.pop(path, None)


class FakeClient:
    def __init__(self, store, kv=None):
        self.kv = kv if kv is not None else FakeKvV2(store)
        self.secrets = type("S", (), {"kv": type("K", (), {"v2": self.kv})()})()


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


class DenyingKvV2(FakeKvV2):
    """A store that refuses every read. Not a 404: Ladon could not look."""

    def read_secret_version(self, *, path, mount_point, raise_on_deleted_version=False):
        raise Forbidden("permission denied")


def build_client(
    sessions,
    store,
    cluster,
    *,
    service_callers,
    clock=None,
    identity=None,
    env=None,
    assertion_verifier=None,
    kv=None,
    raise_server_exceptions=True,
):
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
        client_for=lambda namespace: FakeClient(store, kv=kv),
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
            remove=lambda docs, **kwargs: delete_documents(docs, run=cluster, **kwargs),
            read=lambda doc, **kwargs: read_one(doc, run=cluster, **kwargs),
        ),
        now=clock or (lambda: 0.0),
    )
    app = create_app(
        settings,
        session_factory=sessions,
        secrets=secrets,
        service_lane=lane,
        assertion_verifier=assertion_verifier,
        env=env or {},
    )
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


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


def erase_body(**overrides):
    body = {
        "acting_email": ACTING,
        "namespace": NAMESPACE,
        "path": PATH,
        "apply": False,
    }
    body.update(overrides)
    return body


def sync_body(**overrides):
    body = {
        "acting_email": ACTING,
        "namespace": NAMESPACE,
        "app": APP,
        "path": PATH,
        "name": "sample-demo",
        "apply": False,
    }
    body.update(overrides)
    return body


def unsync_body(**overrides):
    body = {
        "acting_email": ACTING,
        "namespace": NAMESPACE,
        "app": APP,
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


def test_a_kubernetes_namespace_that_is_not_a_label_is_refused():
    with pytest.raises(ConfigError, match="k8s_namespaces"):
        callers(k8s_namespaces=["Not A Label"])


def test_a_caller_gets_a_separate_read_budget_by_default():
    (caller,) = callers()
    assert caller.rate_limit_per_minute == DEFAULT_SERVICE_RATE_LIMIT
    assert caller.read_limit_per_minute == DEFAULT_SERVICE_READ_LIMIT
    # Higher on purpose: a read costs a lookup and changes nothing, and a
    # caller polling one must not spend the budget its writes need.
    assert caller.read_limit_per_minute > caller.rate_limit_per_minute


def test_a_deployment_may_set_the_read_budget():
    (caller,) = callers(read_limit_per_minute=10)
    assert caller.read_limit_per_minute == 10


def test_a_read_budget_that_is_not_a_positive_number_is_refused():
    for bad in (0, -1, "many", True):
        with pytest.raises(ConfigError, match="read_limit_per_minute"):
            callers(read_limit_per_minute=bad)


def test_kubernetes_namespaces_may_be_omitted_entirely():
    config = {k: v for k, v in CALLER_CONFIG.items() if k != "k8s_namespaces"}
    (caller,) = parse_service_callers([config], human_audience=HUMAN_AUDIENCE)
    assert caller.k8s_namespaces == ()


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


def test_a_token_older_than_the_maximum_age_is_refused(client):
    # Still inside its expiry, but minted long ago: a machine token that has
    # been sitting somewhere is a machine token somebody else may be holding.
    stale = service_token(
        claims={"iat": int(now_epoch()) - (MAX_SERVICE_TOKEN_AGE_SECONDS + 60)},
        expires_in=timedelta(hours=6),
    )
    assert put(client, token_value=stale).status_code == 401


def test_a_token_just_inside_the_maximum_age_is_accepted(client):
    fresh = service_token(
        claims={"iat": int(now_epoch()) - (MAX_SERVICE_TOKEN_AGE_SECONDS - 60)},
        expires_in=timedelta(hours=6),
    )
    assert put(client, token_value=fresh).status_code == 200


def test_a_token_issued_in_the_future_is_refused(client):
    ahead = service_token(claims={"iat": int(now_epoch()) + 600})
    assert put(client, token_value=ahead).status_code == 401


def test_a_token_that_is_not_yet_valid_is_refused(client):
    later = service_token(claims={"nbf": int(now_epoch()) + 600})
    assert put(client, token_value=later).status_code == 401


def test_the_lane_keeps_signing_keys_for_a_shorter_grace_than_the_human_lane():
    # That a shorter grace expires keys sooner is proven in test_web_assertion;
    # this is the wiring, which is the part that could silently not happen.
    assert SERVICE_STALE_KEYS_MAX_SECONDS < STALE_KEYS_MAX_SECONDS
    lane = build_service_lane(callers(), fetch=lambda url: jwks((KEY, KID)))
    ((_, verifier),) = lane.verifiers.values()
    assert verifier._jwks._stale_max == SERVICE_STALE_KEYS_MAX_SECONDS


def test_a_token_matching_two_callers_is_refused(sessions, store, cluster):
    # One token naming both audiences: whose subject and namespaces apply is
    # not a question this lane guesses at.
    second_audience = "https://ladon.example.com/service-two"
    pair = parse_service_callers(
        [
            CALLER_CONFIG,
            {**CALLER_CONFIG, "name": "other", "audience": second_audience},
        ],
        human_audience=HUMAN_AUDIENCE,
    )
    ambiguous = build_client(sessions, store, cluster, service_callers=pair)
    both = service_token(audience=[SERVICE_AUDIENCE, second_audience])
    assert put(ambiguous, token_value=both).status_code == 401


def test_a_machine_token_is_not_a_human_identity(sessions, store, cluster):
    # The reverse replay of the audience rule: a machine credential presented
    # where a person's assertion goes must not become that person.
    human = build_client(
        sessions,
        store,
        cluster,
        service_callers=callers(),
        identity=IdentitySettings(assertion=ASSERTION_SETTINGS),
        assertion_verifier=assertion_verifier(),
    )
    headers = {ASSERTION_SETTINGS.header: service_token()}
    assert human.get("/api/namespaces", headers=headers).status_code == 401
    # And the human's own assertion is still not a machine credential.
    assert put(human, token_value=assertion_token()).status_code == 401


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


def test_a_path_that_percent_decodes_to_a_relative_segment_is_refused(client):
    # A hop that decodes before Ladon sees it would read this as "apps/..".
    response = put(client, put_body(path="apps/%2e%2e/platform/demo"))
    assert response.status_code == 400


def test_a_path_carrying_any_percent_escape_is_refused(client):
    response = put(client, put_body(path="apps/sample/de%6do"))
    assert response.status_code == 400


def test_an_unreasonably_long_path_is_refused_on_every_route(client, sessions):
    # Bounded by the request schema, before the value can reach an audit row:
    # an unbounded argument is a way to write a megabyte into the trail with
    # one call. The bound is the store's own, so nothing the path rules would
    # have accepted is refused here.
    long = "apps/sample/" + "a" * MAX_KV_PATH_LENGTH
    assert put(client, put_body(path=long)).status_code == 422
    assert erase(client, erase_body(path=long)).status_code == 422
    assert sync(client, sync_body(path=long)).status_code == 422
    assert read(client, path=long).status_code == 422
    assert rows(sessions, "service.put_secret") == []


def test_a_path_at_the_bound_is_still_refused_by_the_path_rules(client):
    # The schema stops at the length the store allows; what is inside the
    # prefix is still the path rules' decision, answered with a 400.
    at_bound = "apps/sample/" + "a" * (MAX_KV_PATH_LENGTH - len("apps/sample/"))
    assert len(at_bound) == MAX_KV_PATH_LENGTH
    assert put(client, put_body(path=at_bound)).status_code == 200
    assert put(client, put_body(path="platform/" + "a" * 40)).status_code == 400


def test_a_nested_namespace_is_accepted_on_both_routes(sessions, store, cluster):
    nested = build_client(
        sessions, store, cluster, service_callers=callers(namespaces=[NESTED_NAMESPACE])
    )
    assert put(nested, put_body(namespace=NESTED_NAMESPACE)).status_code == 200
    assert sync(nested, sync_body(namespace=NESTED_NAMESPACE)).status_code == 200


def test_a_namespace_that_is_not_a_path_of_labels_is_refused(client):
    assert put(client, put_body(namespace="Team A")).status_code == 422
    assert sync(client, sync_body(namespace="team-a/")).status_code == 422


def test_a_missing_acting_email_is_refused(client):
    body = put_body()
    del body["acting_email"]
    assert client.post("/api/service/secrets", json=body, headers=bearer()).status_code == 422


def test_an_acting_email_that_is_not_an_address_is_refused(client):
    body = put_body(acting_email="not-an-address")
    assert client.post("/api/service/secrets", json=body, headers=bearer()).status_code == 422


def test_an_unreasonably_long_acting_email_is_refused_on_every_route(client, sessions):
    # It matches the address pattern, which says nothing about length, and it
    # is written to an audit row.
    long = "a" * ACTING_EMAIL_MAX + "@example.com"
    assert put(client, put_body(acting_email=long)).status_code == 422
    assert erase(client, erase_body(acting_email=long)).status_code == 422
    assert sync(client, sync_body(acting_email=long)).status_code == 422
    assert unsync(client, unsync_body(acting_email=long)).status_code == 422
    assert read(client, acting_email=long).status_code == 422
    assert rows(sessions, "service.put_secret") == []


def test_an_address_at_the_bound_is_accepted(client):
    at_bound = "a" * (ACTING_EMAIL_MAX - len("@example.com")) + "@example.com"
    assert len(at_bound) == ACTING_EMAIL_MAX
    assert put(client, put_body(acting_email=at_bound)).status_code == 200


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


def test_the_report_never_carries_a_value(client, store):
    written = put_body()["values"]["DB_URL"]
    response = client.post("/api/service/secrets", json=put_body(apply=True), headers=bearer())
    for value in [written, *store[PATH].values()]:
        assert value not in response.text
        assert mask(value) not in response.text
    assert response.json()["written_keys"] == ["DB_URL"]


# ── Sync ───────────────────────────────────────────────────────────────


def test_sync_converges_one_external_secret(client, cluster):
    dry = client.post("/api/service/sync", json=sync_body(), headers=bearer())
    assert dry.status_code == 200
    assert dry.json()["applied"] is False
    assert dry.json()["report"] == ["ExternalSecret/sample/sample-demo: create"]

    applied = client.post("/api/service/sync", json=sync_body(apply=True), headers=bearer())
    assert applied.status_code == 200
    assert applied.json()["report"] == ["ExternalSecret/sample/sample-demo: create"]

    again = client.post("/api/service/sync", json=sync_body(apply=True), headers=bearer())
    assert again.json()["report"] == ["ExternalSecret/sample/sample-demo: already current"]
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


def test_sync_needs_an_acting_email(client):
    body = sync_body()
    del body["acting_email"]
    assert sync(client, body).status_code == 422


# ── Where an ExternalSecret may land ────────────────────────────────────


def test_sync_defaults_the_kubernetes_namespace_to_the_application(client, cluster):
    response = sync(client, sync_body(apply=True))
    assert response.status_code == 200
    assert response.json()["k8s_namespace"] == DEFAULT_K8S_NAMESPACE
    assert f"namespace: {DEFAULT_K8S_NAMESPACE}" in cluster.streams[0]


def test_sync_refuses_a_kubernetes_namespace_the_caller_is_not_pinned_to(client, cluster):
    # The store's selector gates what ESO may READ. It does nothing about where
    # an object may be WRITTEN, and the field manager is Ladon's, so an
    # unpinned namespace here would let one caller create or replace an
    # ExternalSecret anywhere on the cluster.
    response = sync(client, sync_body(k8s_namespace="kube-system", apply=True))
    assert response.status_code == 403
    assert cluster.calls == []


def test_sync_allows_a_kubernetes_namespace_the_caller_is_pinned_to(sessions, store, cluster):
    pinned = build_client(
        sessions, store, cluster, service_callers=callers(k8s_namespaces=[PINNED_K8S_NAMESPACE])
    )
    response = sync(pinned, sync_body(k8s_namespace=PINNED_K8S_NAMESPACE, apply=True))
    assert response.status_code == 200
    assert f"namespace: {PINNED_K8S_NAMESPACE}" in cluster.streams[0]


def test_sync_refuses_a_different_target_name_in_the_default_namespace(client, cluster):
    # Naming a target is how an ExternalSecret adopts a Secret the consumer
    # already uses, which is also how it would take over one it should not.
    response = sync(client, sync_body(target_name="sample-credentials", apply=True))
    assert response.status_code == 403
    assert cluster.calls == []


def test_sync_allows_a_different_target_name_in_a_pinned_namespace(sessions, store, cluster):
    pinned = build_client(
        sessions, store, cluster, service_callers=callers(k8s_namespaces=[PINNED_K8S_NAMESPACE])
    )
    response = sync(
        pinned,
        sync_body(k8s_namespace=PINNED_K8S_NAMESPACE, target_name="sample-credentials", apply=True),
    )
    assert response.status_code == 200
    assert "name: sample-credentials" in cluster.streams[0]


def test_sync_allows_the_same_target_name_spelled_out(client):
    # Naming the target explicitly, identically, is not an override.
    assert sync(client, sync_body(target_name="sample-demo")).status_code == 200


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
    # The row says a machine wrote it, and which one, so a query can tell a
    # machine-on-behalf row from a person's own without matching on tool names.
    assert row.redacted_args["lane"] == "service"
    assert row.redacted_args["via"] == CALLER
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
    assert row.redacted_args["lane"] == "service"
    assert row.redacted_args["via"] == CALLER


def test_a_refused_namespace_is_audited(client, sessions):
    client.post("/api/service/secrets", json=put_body(namespace=OTHER_NAMESPACE), headers=bearer())
    (row,) = rows(sessions, "service.put_secret")
    assert row.status == "denied"
    assert row.redacted_args["namespace"] == OTHER_NAMESPACE
    assert row.redacted_args["via"] == CALLER


def test_a_sync_is_audited(client, sessions):
    client.post("/api/service/sync", json=sync_body(apply=True), headers=bearer())
    (row,) = rows(sessions, "service.sync_secret")
    assert row.status == "ok"
    # Attributed to the person, like a write: the machine is the how, not the who.
    assert row.user_email == ACTING
    assert row.redacted_args["lane"] == "service"
    assert row.redacted_args["via"] == CALLER
    assert row.redacted_args["k8s_namespace"] == DEFAULT_K8S_NAMESPACE


def test_a_denial_with_no_matching_caller_is_audited_as_unknown(client, sessions):
    stray = service_token(audience="https://ladon.example.com/somewhere-else")
    put(client, token_value=stray)
    (row,) = rows(sessions, "service.put_secret")
    assert row.status == "denied"
    assert row.redacted_args["via"] == "unknown"


def test_the_rate_limit_denial_is_audited_under_the_caller(sessions, store, cluster):
    limited = build_client(
        sessions, store, cluster, service_callers=callers(rate_limit_per_minute=1)
    )
    put(limited)
    assert put(limited).status_code == 429
    denied = [row for row in rows(sessions, "service.put_secret") if row.status == "denied"]
    assert len(denied) == 1
    # The caller is known by now, so the row says which machine ran out of budget.
    assert denied[0].user_email == CALLER
    assert denied[0].redacted_args["via"] == CALLER


# ── Reading which keys exist ───────────────────────────────────────────


def test_a_read_returns_the_key_names_the_version_and_the_timestamp(client):
    response = read(client)
    assert response.status_code == 200
    assert response.json() == {
        "namespace": NAMESPACE,
        "path": PATH,
        "keys": ["API_KEY", "DB_URL"],
        "version": 2,
        "updated_at": WRITTEN_AT,
    }


def assert_carries_no_value(response, store):
    """Neither a stored value nor the masked preview of one is in the answer.

    The masks are computed from the fixture rather than written out, so a
    change to what the fixture holds, or to how masking works, cannot leave
    this asserting the absence of a string nothing would have produced.
    """
    for value in store[PATH].values():
        assert value not in response.text
        assert mask(value) not in response.text


def test_a_read_never_returns_a_value_not_even_masked(client, store):
    # The human surface answers with a masked preview of every value. This one
    # does not: a mask is a hint about a value, and this lane hands out names.
    response = read(client)
    assert_carries_no_value(response, store)
    assert "masked_keys" not in response.json()
    # The names themselves are the answer, so this is not vacuous.
    assert response.json()["keys"] == sorted(store[PATH])


def test_a_read_of_a_path_that_is_not_there_is_a_plain_404(client):
    response = read(client, path=f"apps/{APP}/nowhere")
    assert response.status_code == 404
    assert response.json() == {"error": f"there is no secret at {NAMESPACE}/apps/{APP}/nowhere"}


def test_a_read_needs_a_service_token(client):
    assert client.get("/api/service/secrets", params={"namespace": NAMESPACE}).status_code == 401


def test_a_read_needs_an_acting_email(client):
    assert read(client, acting_email=None).status_code == 422


def test_a_read_refuses_a_namespace_the_caller_is_not_pinned_to(client):
    response = read(client, namespace=OTHER_NAMESPACE)
    assert response.status_code == 403
    assert "read" in response.json()["error"]


def test_a_read_refuses_a_path_outside_the_applications_prefix(client):
    assert read(client, path="platform/demo").status_code == 400
    assert read(client, path="apps/../platform/demo").status_code == 400


def test_a_read_is_audited_against_the_human_and_the_caller(client, sessions):
    read(client)
    (row,) = rows(sessions, "service.get_secret")
    assert row.status == "ok"
    assert row.user_email == ACTING
    assert row.redacted_args["lane"] == "service"
    assert row.redacted_args["via"] == CALLER
    assert row.redacted_args["subject"] == SUBJECT
    assert row.redacted_args["path"] == PATH
    assert row.redacted_args["exists"] is True


def test_a_read_that_finds_nothing_is_still_an_audited_read(client, sessions):
    # The request was authorized and the lookup happened. Who asked about a
    # path that does not exist is worth being able to answer.
    read(client, path=f"apps/{APP}/nowhere")
    (row,) = rows(sessions, "service.get_secret")
    assert row.status == "ok"
    assert row.redacted_args["exists"] is False


def test_a_refused_read_is_audited(client, sessions):
    read(client, namespace=OTHER_NAMESPACE)
    (row,) = rows(sessions, "service.get_secret")
    assert row.status == "denied"
    assert row.redacted_args["namespace"] == OTHER_NAMESPACE
    assert row.redacted_args["via"] == CALLER


def test_a_read_spends_the_callers_read_budget(sessions, store, cluster):
    limited = build_client(
        sessions, store, cluster, service_callers=callers(read_limit_per_minute=1)
    )
    assert read(limited).status_code == 200
    assert read(limited).status_code == 429


def test_a_read_does_not_spend_the_write_budget(sessions, store, cluster):
    # A caller polling the read while an application comes up must not find
    # that the write it is waiting to make is the call that gets refused.
    limited = build_client(
        sessions, store, cluster, service_callers=callers(rate_limit_per_minute=1)
    )
    for _ in range(5):
        assert read(limited).status_code == 200
    assert put(limited).status_code == 200
    assert put(limited).status_code == 429
    # And the write budget running out leaves the read working.
    assert read(limited).status_code == 200


def test_the_read_budget_is_its_own_and_does_not_free_a_write(sessions, store, cluster):
    limited = build_client(
        sessions, store, cluster, service_callers=callers(read_limit_per_minute=1)
    )
    assert read(limited).status_code == 200
    assert read(limited).status_code == 429
    # Reads exhausted, writes untouched.
    assert put(limited).status_code == 200


# ── Deleting a whole secret ────────────────────────────────────────────


def test_a_dry_run_delete_says_what_it_would_remove_and_removes_nothing(client, store):
    response = erase(client)
    assert response.status_code == 200
    assert response.json() == {
        "applied": False,
        "namespace": NAMESPACE,
        "path": PATH,
        "keys": ["API_KEY", "DB_URL"],
        "would_delete": True,
    }
    assert PATH in store


def test_an_applied_delete_removes_the_path(client, store):
    response = erase(client, erase_body(apply=True))
    assert response.status_code == 200
    assert response.json() == {
        "applied": True,
        "namespace": NAMESPACE,
        "path": PATH,
        "keys": ["API_KEY", "DB_URL"],
        "deleted": True,
    }
    assert PATH not in store


def test_deleting_the_same_path_twice_is_not_an_error(client, store):
    erase(client, erase_body(apply=True))
    again = erase(client, erase_body(apply=True))
    assert again.status_code == 200
    assert again.json()["deleted"] is False
    assert again.json()["keys"] == []


def test_a_dry_run_delete_of_a_path_that_is_not_there_would_delete_nothing(client):
    response = erase(client, erase_body(path=f"apps/{APP}/nowhere"))
    assert response.status_code == 200
    assert response.json()["would_delete"] is False
    assert response.json()["keys"] == []


def test_a_delete_never_returns_a_value(client, store):
    before = dict(store[PATH])
    response = erase(client, erase_body(apply=True))
    for value in before.values():
        assert value not in response.text
        assert mask(value) not in response.text
    assert response.json()["keys"] == sorted(before)


def test_a_delete_needs_a_service_token(client, store):
    response = client.request("DELETE", "/api/service/secrets", json=erase_body(apply=True))
    assert response.status_code == 401
    assert PATH in store


def test_a_delete_needs_an_acting_email(client):
    body = erase_body()
    del body["acting_email"]
    assert erase(client, body).status_code == 422


def test_a_delete_refuses_a_namespace_the_caller_is_not_pinned_to(client, store):
    response = erase(client, erase_body(namespace=OTHER_NAMESPACE, apply=True))
    assert response.status_code == 403
    assert "delete in" in response.json()["error"]
    assert PATH in store


def test_a_delete_refuses_a_path_outside_the_applications_prefix(client, store):
    assert erase(client, erase_body(path="platform/demo", apply=True)).status_code == 400
    assert erase(client, erase_body(path="apps/../platform/demo", apply=True)).status_code == 400
    assert PATH in store


def test_a_delete_is_audited_with_what_it_removed(client, sessions):
    erase(client, erase_body(apply=True))
    (row,) = rows(sessions, "service.delete_secret")
    assert row.status == "ok"
    assert row.user_email == ACTING
    assert row.redacted_args["lane"] == "service"
    assert row.redacted_args["via"] == CALLER
    assert row.redacted_args["subject"] == SUBJECT
    assert row.redacted_args["path"] == PATH
    # Names, so the row still says what was destroyed once the path is gone.
    assert row.redacted_args["key_names"] == ["API_KEY", "DB_URL"]
    assert row.redacted_args["existed"] is True


def test_a_dry_run_delete_is_audited_as_one(client, sessions):
    erase(client)
    (row,) = rows(sessions, "service.delete_secret")
    assert row.status == "dry-run"


def test_a_refused_delete_is_audited(client, sessions):
    erase(client, erase_body(namespace=OTHER_NAMESPACE, apply=True))
    (row,) = rows(sessions, "service.delete_secret")
    assert row.status == "denied"
    assert row.redacted_args["namespace"] == OTHER_NAMESPACE
    assert row.redacted_args["via"] == CALLER


def test_a_delete_spends_the_callers_budget(sessions, store, cluster):
    limited = build_client(
        sessions, store, cluster, service_callers=callers(rate_limit_per_minute=1)
    )
    assert erase(limited).status_code == 200
    assert erase(limited).status_code == 429


# ── Removing the ExternalSecret ────────────────────────────────────────


def external_secret_is_live(client, cluster):
    """Put one on the cluster the way the lane does, so it can be removed."""
    assert sync(client, sync_body(apply=True)).status_code == 200
    assert len(cluster.objects) == 1


def live_external_secret(
    *,
    managers=("ladon",),
    store=None,
    key=PATH,
    k8s_namespace=DEFAULT_K8S_NAMESPACE,
    name="sample-demo",
    creation_policy="Owner",
    eso_status=True,
):
    """An ExternalSecret as the cluster holds one, with managed fields.

    Defaults to what the POST route renders for this caller's application, so
    a test changes only the one property it is about.
    """
    entries = [managed_field_entry(m) for m in managers]
    if eso_status:
        entries.append(eso_status_entry())
    return {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ExternalSecret",
        "metadata": {
            "name": name,
            "namespace": k8s_namespace,
            "managedFields": entries,
        },
        "spec": {
            "refreshInterval": "1h",
            "secretStoreRef": {
                "name": app_store_name(team=NAMESPACE, app=APP) if store is None else store,
                "kind": "ClusterSecretStore",
            },
            "target": {"name": name, "creationPolicy": creation_policy},
            "dataFrom": [{"extract": {"key": key}}],
        },
    }


def lane_over(sessions, store, live, **overrides):
    """A client whose cluster already holds these objects."""
    cluster = FakeCluster(live=live)
    return build_client(sessions, store, cluster, service_callers=callers(**overrides)), cluster


def test_a_dry_run_unsync_says_what_it_would_remove_and_removes_nothing(client, cluster):
    external_secret_is_live(client, cluster)
    response = unsync(client)
    assert response.status_code == 200
    assert response.json() == {
        "applied": False,
        "namespace": NAMESPACE,
        "app": APP,
        "store": app_store_name(team=NAMESPACE, app=APP),
        "k8s_namespace": DEFAULT_K8S_NAMESPACE,
        "name": "sample-demo",
        "removes_target_secret": True,
        "report": ["ExternalSecret/sample/sample-demo: delete"],
        "would_delete": True,
    }
    assert cluster.deletes == []
    assert len(cluster.objects) == 1


def test_an_applied_unsync_removes_the_external_secret(client, cluster):
    external_secret_is_live(client, cluster)
    response = unsync(client, unsync_body(apply=True))
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert response.json()["report"] == ["ExternalSecret/sample/sample-demo: delete"]
    assert cluster.objects == {}


def test_unsyncing_twice_is_not_an_error(client, cluster):
    external_secret_is_live(client, cluster)
    unsync(client, unsync_body(apply=True))
    again = unsync(client, unsync_body(apply=True))
    assert again.status_code == 200
    assert again.json()["deleted"] is False
    assert again.json()["report"] == ["ExternalSecret/sample/sample-demo: absent"]
    assert len(cluster.deletes) == 1


def test_unsyncing_an_object_that_was_never_there_is_not_an_error(client, cluster):
    response = unsync(client, unsync_body(apply=True))
    assert response.status_code == 200
    assert response.json()["deleted"] is False


def test_unsync_refuses_an_object_a_client_side_apply_has_taken_over(sessions, store):
    # The application's own ExternalSecret, but `kubectl apply` without
    # --server-side has replaced Ladon's managed-fields entry with its own.
    # Whatever maintains it now is not Ladon, and removing it would take the
    # workload's Secret with it.
    taken = live_external_secret(managers=("kubectl-client-side-apply",))
    client, cluster = lane_over(sessions, store, [taken])

    response = unsync(client, unsync_body(apply=True))
    assert response.status_code == 409
    assert "kubectl-client-side-apply" in response.json()["error"]
    assert cluster.deletes == []
    assert len(cluster.objects) == 1


def test_a_dry_run_unsync_refuses_a_taken_over_object_too(sessions, store):
    taken = live_external_secret(managers=("kubectl-client-side-apply",))
    client, _ = lane_over(sessions, store, [taken])
    assert unsync(client).status_code == 409


def test_esos_status_entry_does_not_make_the_object_esos(sessions, store):
    # Every ExternalSecret Ladon applies carries an external-secrets entry for
    # the status subresource. Reading that as ownership would refuse every
    # object on the cluster.
    client, cluster = lane_over(sessions, store, [live_external_secret()])
    assert unsync(client, unsync_body(apply=True)).status_code == 200
    assert cluster.objects == {}


# ── Whose ExternalSecret is it ─────────────────────────────────────────


def test_unsync_refuses_another_applications_object_in_this_callers_reach(sessions, store):
    # The finding this check exists for. `app` is a body field and
    # k8s_namespace defaults to it, so a caller pinned to one OpenBao namespace
    # names another team's application: the object is real, Ladon manages it,
    # and the ownership check alone would let it through. Removing it takes a
    # running workload's Secret with it.
    theirs = live_external_secret(
        store=app_store_name(team="payments", app="payments"),
        key="apps/payments/payments-db",
        k8s_namespace="payments",
        name="payments-db",
    )
    client, cluster = lane_over(sessions, store, [theirs])

    response = unsync(
        client,
        unsync_body(app="payments", k8s_namespace="payments", name="payments-db", apply=True),
    )
    assert response.status_code == 403
    assert cluster.deletes == []
    assert len(cluster.objects) == 1


def test_the_refusal_names_the_stores_and_not_the_path_it_reads(sessions, store):
    theirs = live_external_secret(
        store=app_store_name(team="payments", app="payments"),
        key="apps/payments/payments-db",
        k8s_namespace="payments",
        name="payments-db",
    )
    client, _ = lane_over(sessions, store, [theirs])
    error = unsync(
        client, unsync_body(app="payments", k8s_namespace="payments", name="payments-db")
    ).json()["error"]
    # The store names are configuration and say what is wrong. Which path
    # somebody else's object reads is not this caller's business.
    assert app_store_name(team="payments", app="payments") in error
    assert app_store_name(team=NAMESPACE, app="payments") in error
    assert "payments-db" in error
    assert "apps/payments/payments-db" not in error


def test_unsync_refuses_an_object_that_reads_outside_the_applications_prefix(sessions, store):
    # Right store, wrong path: not something onboarding rendered.
    odd = live_external_secret(key="apps/other/demo")
    client, cluster = lane_over(sessions, store, [odd])
    response = unsync(client, unsync_body(apply=True))
    assert response.status_code == 403
    assert "apps/sample/" in response.json()["error"]
    assert cluster.deletes == []


def test_unsync_refuses_an_object_with_no_extract_at_all(sessions, store):
    bare = live_external_secret()
    del bare["spec"]["dataFrom"]
    client, cluster = lane_over(sessions, store, [bare])
    assert unsync(client, unsync_body(apply=True)).status_code == 403
    assert cluster.deletes == []


def test_the_provenance_check_is_made_before_anything_is_deleted(sessions, store):
    theirs = live_external_secret(store=app_store_name(team="payments", app="sample"))
    client, cluster = lane_over(sessions, store, [theirs])
    unsync(client, unsync_body(apply=True))
    assert cluster.deletes == []


def test_a_refused_provenance_check_is_audited_as_a_denial(sessions, store):
    theirs = live_external_secret(store=app_store_name(team="payments", app="sample"))
    client, _ = lane_over(sessions, store, [theirs])
    unsync(client, unsync_body(apply=True))
    (row,) = rows(sessions, "service.delete_sync")
    assert row.status == "denied"
    assert row.redacted_args["via"] == CALLER
    assert row.redacted_args["store"] == app_store_name(team=NAMESPACE, app=APP)


def test_an_object_that_does_not_own_its_target_says_so(sessions, store):
    # Merge or None leaves the Secret behind, and the answer must not claim
    # otherwise.
    shared = live_external_secret(creation_policy="Merge")
    client, _ = lane_over(sessions, store, [shared])
    response = unsync(client, unsync_body(apply=True))
    assert response.status_code == 200
    assert response.json()["removes_target_secret"] is False


def test_removing_nothing_removes_no_target_secret(client, cluster):
    response = unsync(client, unsync_body(apply=True))
    assert response.json()["removes_target_secret"] is False
    assert response.json()["deleted"] is False


def test_unsync_needs_a_service_token(client):
    assert client.request("DELETE", "/api/service/sync", json=unsync_body()).status_code == 401


def test_unsync_needs_an_acting_email(client):
    body = unsync_body()
    del body["acting_email"]
    assert unsync(client, body).status_code == 422


def test_unsync_refuses_a_namespace_the_caller_is_not_pinned_to(client, cluster):
    response = unsync(client, unsync_body(namespace=OTHER_NAMESPACE, apply=True))
    assert response.status_code == 403
    assert cluster.calls == []


def test_unsync_refuses_a_kubernetes_namespace_the_caller_is_not_pinned_to(client, cluster):
    # The same gate the POST has: this process holds the ladon field manager,
    # so an unpinned namespace here would let one caller remove an
    # ExternalSecret anywhere on the cluster that Ladon happens to manage.
    response = unsync(client, unsync_body(k8s_namespace="kube-system", apply=True))
    assert response.status_code == 403
    assert cluster.calls == []


def test_unsync_allows_a_kubernetes_namespace_the_caller_is_pinned_to(sessions, store, cluster):
    pinned = build_client(
        sessions, store, cluster, service_callers=callers(k8s_namespaces=[PINNED_K8S_NAMESPACE])
    )
    placed = sync(pinned, sync_body(k8s_namespace=PINNED_K8S_NAMESPACE, apply=True))
    assert placed.status_code == 200
    response = unsync(pinned, unsync_body(k8s_namespace=PINNED_K8S_NAMESPACE, apply=True))
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert cluster.objects == {}


def test_unsync_leaves_the_store_and_the_identity_alone(client, cluster):
    # Application off-boarding is not this route. Only the ExternalSecret is
    # named, and only one delete runs.
    external_secret_is_live(client, cluster)
    unsync(client, unsync_body(apply=True))
    assert [call[2] for call in cluster.deletes] == [
        "externalsecret.external-secrets.io/sample-demo"
    ]


def test_an_unsync_is_audited(client, sessions, cluster):
    external_secret_is_live(client, cluster)
    unsync(client, unsync_body(apply=True))
    (row,) = rows(sessions, "service.delete_sync")
    assert row.status == "ok"
    assert row.user_email == ACTING
    assert row.redacted_args["lane"] == "service"
    assert row.redacted_args["via"] == CALLER
    assert row.redacted_args["subject"] == SUBJECT
    assert row.redacted_args["k8s_namespace"] == DEFAULT_K8S_NAMESPACE
    assert row.redacted_args["name"] == "sample-demo"
    assert row.redacted_args["outcome"] == "delete"
    assert row.redacted_args["removes_target_secret"] is True


def test_a_dry_run_unsync_is_audited_as_one(client, sessions, cluster):
    external_secret_is_live(client, cluster)
    unsync(client)
    (row,) = rows(sessions, "service.delete_sync")
    assert row.status == "dry-run"


def test_an_unsync_refused_for_a_foreign_object_is_audited(sessions, store):
    taken = live_external_secret(managers=("kubectl-client-side-apply",))
    client, _ = lane_over(sessions, store, [taken])
    unsync(client, unsync_body(apply=True))
    (row,) = rows(sessions, "service.delete_sync")
    assert row.status == "refused"
    assert row.redacted_args["via"] == CALLER


def test_an_unsync_spends_the_callers_budget(sessions, store, cluster):
    limited = build_client(
        sessions, store, cluster, service_callers=callers(rate_limit_per_minute=1)
    )
    assert unsync(limited).status_code == 200
    assert unsync(limited).status_code == 429


# ── When OpenBao cannot be read ────────────────────────────────────────


@pytest.fixture
def denied(sessions, store, cluster):
    """A lane whose vault refuses every read."""
    # Errors are rendered rather than re-raised, which is what a real caller
    # sees: the application's handler turns an unexpected failure into a 500.
    return build_client(
        sessions,
        store,
        cluster,
        service_callers=callers(),
        kv=DenyingKvV2(store),
        raise_server_exceptions=False,
    )


def test_a_read_that_openbao_refuses_is_not_a_404(denied):
    # "I could not look" is never "there is nothing there": answering 404 here
    # would report a secret that exists as gone.
    response = read(denied)
    assert response.status_code == 500
    assert response.json() == {"error": "internal error"}


def test_a_read_that_openbao_refuses_is_audited_as_an_error(denied, sessions):
    read(denied)
    (row,) = rows(sessions, "service.get_secret")
    assert row.status == "error"
    assert row.redacted_args["via"] == CALLER


def test_a_delete_whose_read_fails_does_not_report_a_deletion(denied, store):
    # The answer would otherwise be `deleted: false` and a 200, which tells a
    # caller a path is gone when nothing was even looked at.
    response = erase(denied, erase_body(apply=True))
    assert response.status_code == 500
    assert PATH in store


def test_a_delete_whose_read_fails_is_audited_as_an_error(denied, sessions):
    erase(denied, erase_body(apply=True))
    (row,) = rows(sessions, "service.delete_secret")
    assert row.status == "error"


def test_the_delete_runs_whether_or_not_the_read_found_anything(client, store):
    # Unconditional on apply: OpenBao's metadata delete answers the same either
    # way, so skipping it on the strength of an earlier read only adds a race.
    response = erase(client, erase_body(path=f"apps/{APP}/nowhere", apply=True))
    assert response.status_code == 200
    assert response.json()["deleted"] is False
