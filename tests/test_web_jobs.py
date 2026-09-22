"""Tests for the apply endpoints and the jobs they run (D14).

An apply phase outlives a load balancer's timeout, so the endpoints start a
background job and answer with its id. These run against a TestClient over
SQLite, with the OpenBao client and the cluster runner faked at the same seams
the CLI tests use, so nothing dials a vault or a cluster.

The jobs are real threads, so the tests poll the job route the way a caller
does rather than reaching into the runner.
"""

import json
import threading
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from fake_cluster import FakeCluster
from fake_openbao import FakeOpenBao
from ladon.apply import cli
from ladon.config.loader import load_deployment_config
from ladon.datastore import repositories as repo
from ladon.datastore.models import Base
from ladon.install.eso import ESOInstallResult
from ladon.install.k8s import ESOInstall, converge_documents
from ladon.web.app import create_app
from ladon.web.jobs import JobOutcome, JobRunner
from ladon.web.settings import (
    DEFAULT_IDENTITY_HEADER,
    IdentitySettings,
    WebSettings,
    parse_access,
)

ADMIN = "admin@example.com"
MEMBER = "op@example.com"
HEADER = DEFAULT_IDENTITY_HEADER
ADDRESS = "https://vault.example:8200"

# The deployment's own config. The endpoints never take one from a request.
CONFIG_YAML = """
teams:
  - name: team-a
    environments: [dev]
  - name: team-b
    environments: [dev]
"""

# The application identity and store the app-onboarding phase renders for
# team-a's "sample" application; see tests/test_config_apps.py.
APP_IDENTITY = "eso-team-a-sample-a84085"
APP_STORE = "ladon-team-a-sample-a84085"

ESO_RUNNING = ESOInstall(
    namespace="external-secrets", service_account="external-secrets", version="v0.19.2"
)


@pytest.fixture
def sessions(tmp_path):
    # A file, not ":memory:": a job runs on its own connection while the
    # request's is still open, which only a real file exercises.
    engine = create_engine(f"sqlite:///{tmp_path / 'ladon.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "deployment.yaml"
    path.write_text(CONFIG_YAML)
    return load_deployment_config(str(path))


@pytest.fixture
def cluster(monkeypatch):
    """A fake cluster with an operator on it, behind the kubectl seam."""
    fake = FakeCluster()
    monkeypatch.setattr(cli, "detect_eso", lambda: ESO_RUNNING)
    monkeypatch.setattr(cli, "eso_api_version", lambda: "v1")
    monkeypatch.setattr(
        cli,
        "converge_documents",
        lambda docs, **kwargs: converge_documents(docs, run=fake, **kwargs),
    )
    return fake


@pytest.fixture
def vault(monkeypatch):
    """A fake OpenBao, and a record of how the job dialled it."""
    fake = FakeOpenBao()
    fake.dialled = []
    monkeypatch.setenv("BAO_TOKEN", "s.a-real-looking-token")
    monkeypatch.delenv("BAO_ADDR", raising=False)
    monkeypatch.delenv("VAULT_ADDR", raising=False)

    def connect(conn, credential, **kwargs):
        fake.dialled.append((conn.address, credential.token))
        return fake

    monkeypatch.setattr(cli, "connect", connect)
    return fake


def build_settings(config, *, address=ADDRESS):
    return WebSettings(
        identity=IdentitySettings(),
        access=parse_access({"admins": [ADMIN], "members": {MEMBER: ["team-a"]}}),
        environment="dev",
        openbao_address=address,
        kv_mount="kv",
        ui_dist=None,
        reveal_enabled=False,
        deployment=config,
    )


@pytest.fixture
def client(sessions, config):
    app = create_app(build_settings(config), session_factory=sessions, secrets=object())
    return TestClient(app)


def as_admin(client, method, path, **kwargs):
    return client.request(method, path, headers={HEADER: ADMIN}, **kwargs)


def start(client, phase, **body):
    return as_admin(client, "POST", f"/api/admin/apply/{phase}", json=body)


def finished(client, job_id, *, timeout=10.0):
    """Poll the job route until it reaches a final state, as a caller would."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = as_admin(client, "GET", f"/api/admin/jobs/{job_id}").json()
        if job["state"] in ("done", "failed"):
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish: {job}")


def app_body(**extra):
    return {"team": "team-a", "app": "sample", "cluster": "cluster-a", **extra}


# The happy path ---------------------------------------------------------------


def test_a_dry_run_app_onboarding_job_reports_the_diff(client, vault, cluster):
    started = start(client, "app-onboarding", **app_body())
    assert started.status_code == 202
    assert started.json() == {"job_id": 1, "kind": "app-onboarding", "state": "queued"}

    job = finished(client, started.json()["job_id"])

    assert job["state"] == "done"
    assert job["error"] is None
    assert job["kind"] == "app-onboarding"
    assert job["requested_by"] == ADMIN
    assert job["report"] == [
        {
            "surface": "openbao",
            "action": "create",
            "target": "[team-a] write-policy sys/policies/acl/eso-sample-readonly",
        },
        {
            "surface": "openbao",
            "action": "create",
            "target": f"[team-a] write-auth-role auth/jwt-cluster-a/role/{APP_IDENTITY}",
        },
        {
            "surface": "cluster",
            "action": "create",
            "target": f"ServiceAccount/ladon/{APP_IDENTITY}",
        },
        {
            "surface": "cluster",
            "action": "create",
            "target": f"ClusterSecretStore/{APP_STORE}",
        },
    ]
    # A dry run is a diff: nothing was written to either half.
    assert vault.calls == []
    assert cluster.objects == {}
    # Server-computed, and the timestamps are there for a client that wants to
    # show its own.
    assert job["elapsed_seconds"] >= 0
    assert job["created_at"] and job["started_at"] and job["finished_at"]


def test_an_applied_job_writes_and_a_second_one_is_already_current(client, vault, cluster):
    started = start(client, "app-onboarding", apply=True, **app_body())
    first = finished(client, started.json()["job_id"])
    assert first["state"] == "done"
    assert [entry["action"] for entry in first["report"]] == ["create"] * 4
    assert "eso-sample-readonly" in vault.state("team-a").policies

    second = finished(
        client, start(client, "app-onboarding", apply=True, **app_body()).json()["job_id"]
    )
    assert [entry["action"] for entry in second["report"]] == ["already current"] * 4


def test_a_root_config_job_narrowed_to_one_team_runs(client, vault, cluster):
    started = start(client, "root-config", teams=["team-b"], apply=True)
    job = finished(client, started.json()["job_id"])
    assert job["state"] == "done"
    assert [entry["target"] for entry in job["report"]] == [
        "ensure-namespace sys/namespaces/team-b",
        "[team-b] enable-secrets-engine sys/mounts/kv",
        "[team-b] write-policy sys/policies/acl/team-readwrite",
        "[team-b] write-policy sys/policies/acl/external-secrets-read",
    ]


def test_an_eso_install_job_reuses_the_operator_on_the_cluster(client, monkeypatch):
    monkeypatch.setattr(
        cli,
        "install_eso",
        lambda release, **kwargs: ESOInstallResult(
            summary="reusing external-secrets v0.19.2 in namespace external-secrets",
            reused=True,
            existing=ESO_RUNNING,
            api_version="v1",
        ),
    )
    job = finished(client, start(client, "eso-install").json()["job_id"])
    assert job["state"] == "done"
    assert job["report"] == [
        {
            "surface": "cluster",
            "action": "already current",
            "target": "external-secrets operator",
            "reason": "reusing external-secrets v0.19.2 in namespace external-secrets",
        }
    ]


# One job per kind --------------------------------------------------------------


@pytest.fixture
def held(monkeypatch):
    """A vault connection that blocks until the test lets it go."""
    release = threading.Event()
    monkeypatch.setenv("BAO_TOKEN", "s.a-real-looking-token")

    def connect(conn, credential, **kwargs):
        release.wait(timeout=10)
        return FakeOpenBao()

    monkeypatch.setattr(cli, "connect", connect)
    return release


def test_a_second_job_of_the_same_kind_answers_409_with_the_running_id(client, held, cluster):
    first = start(client, "app-onboarding", **app_body())
    assert first.status_code == 202

    second = start(client, "app-onboarding", **app_body())
    assert second.status_code == 409
    body = second.json()
    assert body["job_id"] == first.json()["job_id"]
    assert "app-onboarding" in body["error"]

    held.set()
    assert finished(client, first.json()["job_id"])["state"] == "done"


def test_a_job_of_another_kind_starts_while_one_runs(client, held, cluster):
    app = start(client, "app-onboarding", **app_body())
    root = start(client, "root-config", teams=["team-b"])
    assert (app.status_code, root.status_code) == (202, 202)
    assert root.json()["kind"] == "root-config"

    held.set()
    for response in (app, root):
        assert finished(client, response.json()["job_id"])["state"] == "done"


def test_a_kind_is_free_again_once_its_job_has_finished(client, vault, cluster):
    first = start(client, "app-onboarding", **app_body())
    assert finished(client, first.json()["job_id"])["state"] == "done"
    assert start(client, "app-onboarding", **app_body()).status_code == 202


# The job's own credential ------------------------------------------------------


def test_the_job_dials_the_deployments_own_vault_with_the_token_from_the_environment(
    client, vault, cluster
):
    finished(client, start(client, "app-onboarding", **app_body()).json()["job_id"])
    assert vault.dialled == [(ADDRESS, "s.a-real-looking-token")]


# Failures ----------------------------------------------------------------------


def test_a_job_that_raises_is_failed_with_a_redacted_error(client, cluster, monkeypatch):
    monkeypatch.setenv("BAO_TOKEN", "s.a-real-looking-token")

    def boom(conn, credential, **kwargs):
        raise RuntimeError(
            "OpenBao refused the token AbCdEf0123456789AbCdEf0123456789AbCdEf0123456789"
        )

    monkeypatch.setattr(cli, "connect", boom)

    job = finished(client, start(client, "app-onboarding", **app_body()).json()["job_id"])
    assert job["state"] == "failed"
    assert "OpenBao refused the token" in job["error"]
    assert "AbCdEf0123456789" not in job["error"]
    assert job["report"] == []


def test_a_blocked_operation_fails_the_job_and_keeps_the_report(client, cluster, monkeypatch):
    # An engine already mounted as something else cannot converge. Nothing is
    # written, and the report says which object and why.
    monkeypatch.setenv("BAO_TOKEN", "s.a-real-looking-token")
    fake = FakeOpenBao()
    fake.state("team-b").mounts["kv/"] = {"type": "cubbyhole", "options": {}}
    monkeypatch.setattr(cli, "connect", lambda conn, credential, **k: fake)

    started = start(client, "root-config", teams=["team-b"], apply=True)
    job = finished(client, started.json()["job_id"])
    assert job["state"] == "failed"
    assert "blocked" in job["error"]
    blocked = [entry for entry in job["report"] if entry["action"] == "blocked"]
    assert blocked and "reason" in blocked[0]
    assert fake.calls == []


# The surface itself ------------------------------------------------------------


def test_only_an_admin_can_start_a_job_or_read_one(client, vault, cluster):
    started = start(client, "app-onboarding", **app_body())
    for method, path in (
        ("POST", "/api/admin/apply/app-onboarding"),
        ("GET", f"/api/admin/jobs/{started.json()['job_id']}"),
        ("GET", "/api/admin/jobs"),
    ):
        answer = client.request(method, path, headers={HEADER: MEMBER}, json={})
        assert answer.status_code == 403, path


def test_an_unknown_phase_is_not_a_job(client):
    assert start(client, "everything").status_code == 404


def test_a_request_cannot_name_a_configuration(client):
    # The deployment's own config is the one Ladon runs with. A body that names
    # another is refused rather than ignored.
    answer = as_admin(
        client, "POST", "/api/admin/apply/root-config", json={"config": "/tmp/other.yaml"}
    )
    assert answer.status_code == 422


def test_a_phase_missing_an_input_is_refused_before_a_job_exists(client, sessions):
    answer = start(client, "app-onboarding", team="team-a", cluster="cluster-a")
    assert answer.status_code == 400
    assert "app" in answer.json()["error"]
    with sessions() as session:
        assert repo.list_jobs(session) == []


def test_a_phase_naming_a_team_the_config_does_not_define_is_refused(client):
    answer = start(client, "root-config", teams=["team-z"])
    assert answer.status_code == 400
    assert "team-z" in answer.json()["error"]


def test_an_unknown_job_is_404(client):
    assert as_admin(client, "GET", "/api/admin/jobs/999").status_code == 404


def test_jobs_are_listed_newest_first_and_can_be_filtered_by_kind(client, vault, cluster):
    finished(client, start(client, "app-onboarding", **app_body()).json()["job_id"])
    finished(client, start(client, "root-config", teams=["team-b"]).json()["job_id"])

    listed = as_admin(client, "GET", "/api/admin/jobs").json()["jobs"]
    assert [job["kind"] for job in listed] == ["root-config", "app-onboarding"]

    only = as_admin(client, "GET", "/api/admin/jobs?kind=app-onboarding&limit=1").json()["jobs"]
    assert [job["kind"] for job in only] == ["app-onboarding"]


def test_starting_a_job_is_audited(client, vault, cluster, sessions):
    start(client, "app-onboarding", **app_body())
    with sessions() as session:
        (row,) = repo.recent_calls(session)
    assert (row.tool_name, row.status, row.user_email) == ("apply_app-onboarding", "ok", ADMIN)
    assert row.redacted_args["kind"] == "app-onboarding"
    assert row.redacted_args["args"]["team"] == "team-a"


# Approving a namespace request -------------------------------------------------


def open_request(sessions, team="team-b"):
    with sessions() as session:
        row = repo.create_namespace_request(
            session,
            requester_email=MEMBER,
            team_name=team,
            environments=["dev"],
            members=[MEMBER],
        )
        session.commit()
        return row.id


def test_approving_a_request_applies_that_one_teams_namespace(client, sessions, vault, cluster):
    request_id = open_request(sessions)

    approved = as_admin(client, "POST", f"/api/admin/namespace-requests/{request_id}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    job_id = approved.json()["job_id"]

    job = finished(client, job_id)
    assert job["kind"] == "root-config"
    assert job["state"] == "done"
    assert [entry["target"] for entry in job["report"]] == [
        "ensure-namespace sys/namespaces/team-b",
        "[team-b] enable-secrets-engine sys/mounts/kv",
        "[team-b] write-policy sys/policies/acl/team-readwrite",
        "[team-b] write-policy sys/policies/acl/external-secrets-read",
    ]
    # Applied, not a dry run: the namespace has to exist for the team to use it.
    assert "team-b" in vault.namespaces


def test_a_failed_job_leaves_the_request_approved_with_the_error_on_it(
    client, sessions, cluster, monkeypatch
):
    monkeypatch.setenv("BAO_TOKEN", "s.a-real-looking-token")
    monkeypatch.setattr(
        cli, "connect", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("OpenBao is sealed"))
    )
    request_id = open_request(sessions)

    approved = as_admin(client, "POST", f"/api/admin/namespace-requests/{request_id}/approve")
    assert finished(client, approved.json()["job_id"])["state"] == "failed"

    listed = as_admin(client, "GET", "/api/admin/namespace-requests").json()["requests"]
    (row,) = [r for r in listed if r["id"] == request_id]
    # The decision stands; what failed is the run, and the row says so.
    assert row["status"] == "approved"
    assert "OpenBao is sealed" in row["error"]
    assert row["job_id"] == approved.json()["job_id"]


def test_approving_a_request_for_a_team_the_config_does_not_define_is_refused(client, sessions):
    request_id = open_request(sessions, team="team-z")
    answer = as_admin(client, "POST", f"/api/admin/namespace-requests/{request_id}/approve")
    assert answer.status_code == 400
    assert "team-z" in answer.json()["error"]
    with sessions() as session:
        # Nothing was decided: an approval that cannot be applied is not one.
        assert repo.list_namespace_requests(session)[0].status == "pending"


def test_a_report_names_objects_and_carries_no_payload(client, vault, cluster):
    # The report is what an operator reads, and it lands in the datastore: it
    # names the object each operation touched, never a token and never a body.
    job = finished(
        client, start(client, "app-onboarding", apply=True, **app_body()).json()["job_id"]
    )
    text = json.dumps(job)
    assert "s.a-real-looking-token" not in text
    # The policy body (its HCL) is a payload, so it never reaches the report.
    assert "capabilities" not in text
    assert "kv/data/apps/sample" not in text
    assert "sys/policies/acl/eso-sample-readonly" in text


# What a restart leaves behind ---------------------------------------------------


def test_a_restart_fails_the_jobs_that_were_in_flight(sessions, config, vault, cluster, caplog):
    # A job is a thread inside one process: a restart takes it with it. The row
    # it left saying "running" is both untrue and, because one job of a kind
    # runs at a time, enough to refuse that kind for good.
    with sessions() as session:
        queued = repo.create_job(session, kind="app-onboarding", requested_by=ADMIN)
        running = repo.create_job(session, kind="root-config", requested_by=ADMIN)
        repo.mark_job_running(session, job_id=running.id)
        session.commit()
        ids = (queued.id, running.id)

    restarted = TestClient(
        create_app(build_settings(config), session_factory=sessions, secrets=object())
    )

    for job_id in ids:
        job = as_admin(restarted, "GET", f"/api/admin/jobs/{job_id}").json()
        assert job["state"] == "failed"
        assert job["error"] == "interrupted by a restart"
    # And the kinds they held are free again.
    assert start(restarted, "app-onboarding", **app_body()).status_code == 202


def test_a_finish_hook_that_raises_does_not_lose_the_jobs_state(sessions):
    # The hook is somebody else's bookkeeping. It runs after the state is
    # recorded, and in its own transaction, so it cannot undo it.
    runner = JobRunner(sessions)

    def explode(session, job):
        raise RuntimeError("the hook is broken")

    job_id = runner.submit(
        kind="root-config",
        requested_by=ADMIN,
        work=lambda: JobOutcome(
            report=({"surface": "openbao", "action": "create", "target": "x"},)
        ),
        on_finish=explode,
    )

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with sessions() as session:
            state = repo.get_job(session, job_id).state
        if state in ("done", "failed"):
            break
        time.sleep(0.01)
    assert state == "done"
    with sessions() as session:
        assert repo.get_job(session, job_id).report_json["operations"][0]["target"] == "x"


# Bodies that are refused before a job exists ------------------------------------


def test_an_address_carrying_credentials_is_refused_and_never_stored(client, sessions):
    answer = start(client, "app-onboarding", server="https://user:pw@vault.example", **app_body())
    assert answer.status_code == 400
    assert "credential" in answer.json()["error"]
    # The refusal does not repeat what it found, and nothing was recorded.
    assert "pw" not in answer.text
    with sessions() as session:
        assert repo.list_jobs(session) == []
        assert [row.tool_name for row in repo.recent_calls(session)] == []


def test_a_field_belonging_to_another_phase_is_refused(client, sessions):
    answer = start(client, "team-onboarding", team="team-a", cluster="cluster-a", app="sample")
    assert answer.status_code == 400
    assert "app" in answer.json()["error"]
    assert "team-onboarding" in answer.json()["error"]
    with sessions() as session:
        assert repo.list_jobs(session) == []


def test_eso_install_refuses_a_body_meant_for_another_phase(client):
    answer = start(client, "eso-install", team="team-a")
    assert answer.status_code == 400
    assert "team" in answer.json()["error"]


def test_the_audit_row_names_the_inputs_and_not_the_secret_reference(
    client, vault, cluster, sessions
):
    start(
        client,
        "root-config",
        teams=["team-b"],
        jwt_discovery_url="https://issuer.example/.well-known/openid-configuration",
        oidc_discovery_url="https://issuer.example/.well-known/openid-configuration",
        oidc_client_id="ladon",
        oidc_client_secret_ref="kv/data/ladon/oidc#client_secret",
    )
    with sessions() as session:
        (row,) = repo.recent_calls(session)
    args = row.redacted_args["args"]
    assert args["teams"] == ["team-b"]
    assert args["oidc_client_id"] == "ladon"
    # A reference to a secret is still a way to find one, and it is not needed
    # to know what was asked for.
    assert "oidc_client_secret_ref" not in args
    assert "client_secret" not in str(row.redacted_args)


# The operator install -----------------------------------------------------------


def use_install(monkeypatch, *, reused=False, summary="dry run: would install external-secrets"):
    calls = {}

    def install_eso(release, **kwargs):
        calls.update(namespace=release.namespace, apply=kwargs.get("apply"))
        return ESOInstallResult(summary=summary, reused=reused, api_version="v1")

    monkeypatch.setattr(cli, "install_eso", install_eso)
    return calls


def test_a_dry_run_eso_install_never_claims_to_have_installed(client, monkeypatch):
    calls = use_install(monkeypatch)
    job = finished(client, start(client, "eso-install").json()["job_id"])
    assert calls["apply"] is False
    assert [entry["action"] for entry in job["report"]] == ["would install"]


def test_an_applied_eso_install_says_it_installed(client, monkeypatch):
    use_install(monkeypatch, summary="installed external-secrets chart 2.6.0")
    job = finished(client, start(client, "eso-install", apply=True).json()["job_id"])
    assert [entry["action"] for entry in job["report"]] == ["install"]


def test_an_operator_already_there_is_already_current_on_an_apply_too(client, monkeypatch):
    use_install(monkeypatch, reused=True, summary="reusing external-secrets v0.19.2")
    job = finished(client, start(client, "eso-install", apply=True).json()["job_id"])
    assert [entry["action"] for entry in job["report"]] == ["already current"]


# Deciding a request -------------------------------------------------------------


def test_a_request_that_is_not_pending_cannot_be_decided_again(client, sessions, vault, cluster):
    request_id = open_request(sessions)
    first = as_admin(client, "POST", f"/api/admin/namespace-requests/{request_id}/approve")
    assert first.status_code == 200

    again = as_admin(client, "POST", f"/api/admin/namespace-requests/{request_id}/approve")
    assert again.status_code == 400
    assert "approved" in again.json()["error"]

    rejected = as_admin(
        client,
        "POST",
        f"/api/admin/namespace-requests/{request_id}/reject",
        json={"reason": "changed my mind"},
    )
    assert rejected.status_code == 400


def test_a_rejected_request_cannot_then_be_approved(client, sessions):
    request_id = open_request(sessions)
    as_admin(
        client,
        "POST",
        f"/api/admin/namespace-requests/{request_id}/reject",
        json={"reason": "not this quarter"},
    )
    answer = as_admin(client, "POST", f"/api/admin/namespace-requests/{request_id}/approve")
    assert answer.status_code == 400
    assert "rejected" in answer.json()["error"]


def test_an_approval_stands_even_when_its_run_cannot_start(client, sessions, held, cluster):
    # A root-config job is already in flight, so this one cannot claim the kind.
    request_id = open_request(sessions)
    assert start(client, "root-config", teams=["team-b"]).status_code == 202

    answer = as_admin(client, "POST", f"/api/admin/namespace-requests/{request_id}/approve")
    assert answer.status_code == 409

    listed = as_admin(client, "GET", "/api/admin/namespace-requests").json()["requests"]
    (row,) = [r for r in listed if r["id"] == request_id]
    # The decision was recorded before the run was attempted, so it stands, and
    # the row says why nothing is running for it.
    assert row["status"] == "approved"
    assert "already running" in row["error"]
    assert row["job_id"] is None

    held.set()
