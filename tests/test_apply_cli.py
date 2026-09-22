"""Tests for the apply CLI entrypoint (D3b, D18).

The ``plan`` path is pure (no connection). The ``apply`` path connects in both
modes (a dry-run reads live state to diff against it), so its connect and
execute calls are monkeypatched, or pointed at the recording fake, and nothing
touches a live OpenBao. The cluster half is pointed at the fake cluster the
same way, so both halves of an onboarding are exercised end to end offline.
"""

import argparse
import io

import pytest
import yaml

from fake_cluster import FakeCluster
from fake_openbao import FakeOpenBao
from ladon.apply import cli
from ladon.apply.executor import ApplyResult, OpDiff
from ladon.config.loader import load_deployment_config
from ladon.config.teams import ConfigError
from ladon.install.eso import ESOInstallResult
from ladon.install.k8s import ESOInstall, K8sError, converge_documents

ISSUER = "https://issuer.example/.well-known/openid-configuration"


def run(argv):
    out = io.StringIO()
    code = cli.main(argv, out=out)
    return code, out.getvalue()


def refuse_to_connect(*a, **k):
    raise AssertionError("plan must never connect to OpenBao")


def test_plan_root_config_renders_without_connecting(monkeypatch):
    # Proven by refusing the connection rather than by the absence of an
    # address: the environment may well have one set.
    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    monkeypatch.setenv("BAO_TOKEN", "s.realtoken")
    monkeypatch.setattr(cli, "connect", refuse_to_connect)
    code, text = run(["plan", "--phase", "root-config", "--jwt-discovery-url", ISSUER])
    assert code == 0
    assert "enable-secrets-engine sys/mounts/kv" in text
    assert "write-policy sys/policies/acl/team-readwrite" in text
    assert "enable-auth-method sys/auth/jwt" in text


def test_plan_root_config_with_oidc():
    code, text = run(
        [
            "plan",
            "--phase",
            "root-config",
            "--jwt-discovery-url",
            ISSUER,
            "--oidc-discovery-url",
            ISSUER,
            "--oidc-client-id",
            "ladon",
            "--oidc-client-secret-ref",
            "secret/oidc",
        ]
    )
    assert code == 0
    assert "enable-auth-method sys/auth/oidc" in text
    # The secret reference never appears in the rendered plan.
    assert "secret/oidc" not in text


def test_oidc_without_client_id_is_rejected():
    code, _ = run(
        [
            "plan",
            "--phase",
            "root-config",
            "--jwt-discovery-url",
            ISSUER,
            "--oidc-discovery-url",
            ISSUER,
        ]
    )
    assert code == 2  # missing --oidc-client-id / --oidc-client-secret-ref


def test_plan_team_onboarding_renders(monkeypatch):
    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    monkeypatch.setenv("BAO_TOKEN", "s.realtoken")
    monkeypatch.setattr(cli, "connect", refuse_to_connect)
    code, text = run(
        [
            "plan",
            "--phase",
            "team-onboarding",
            "--cluster",
            "team-a",
            "--oidc-discovery-url",
            ISSUER,
        ]
    )
    assert code == 0
    assert "enable-auth-method sys/auth/jwt-team-a" in text
    assert "write-auth-role auth/jwt-team-a/role/external-secrets-root" in text


def test_root_config_requires_jwt_discovery_url():
    code, _ = run(["plan", "--phase", "root-config"])
    assert code == 2


def test_team_onboarding_requires_cluster():
    code, _ = run(["plan", "--phase", "team-onboarding", "--oidc-discovery-url", ISSUER])
    assert code == 2


def test_apply_without_flag_diffs_against_live_state_and_writes_nothing(monkeypatch):
    # The dry-run connects (read-only) so the diff is against what is really
    # there, which is the whole point of a diff.
    client = FakeOpenBao()
    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    monkeypatch.setenv("BAO_TOKEN", "s.realtoken")
    monkeypatch.setattr(cli, "connect", lambda conn, cred, **k: client)

    code, text = run(["apply", "--phase", "root-config", "--jwt-discovery-url", ISSUER])
    assert code == 0
    assert "DRY RUN" in text
    assert "Re-run with --apply" in text
    assert "create enable-secrets-engine sys/mounts/kv" in text
    assert client.calls == []


def test_apply_dry_run_reports_objects_that_are_already_current(monkeypatch):
    client = FakeOpenBao()
    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    monkeypatch.setenv("BAO_TOKEN", "s.realtoken")
    monkeypatch.setattr(cli, "connect", lambda conn, cred, **k: client)

    run(["apply", "--phase", "root-config", "--jwt-discovery-url", ISSUER, "--apply"])
    code, text = run(["apply", "--phase", "root-config", "--jwt-discovery-url", ISSUER])
    assert code == 0
    assert "already current enable-secrets-engine sys/mounts/kv" in text


def test_apply_reports_when_there_is_nothing_to_do(monkeypatch):
    client = FakeOpenBao()
    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    monkeypatch.setenv("BAO_TOKEN", "s.realtoken")
    monkeypatch.setattr(cli, "connect", lambda conn, cred, **k: client)

    argv = ["apply", "--phase", "root-config", "--jwt-discovery-url", ISSUER, "--apply"]
    run(argv)
    code, text = run(argv)
    assert code == 0
    assert "Nothing to apply" in text


def test_apply_reports_every_op_when_one_is_blocked_and_exits_non_zero(monkeypatch):
    client = FakeOpenBao(mounts={"kv/": {"type": "cubbyhole", "options": {}}})
    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    monkeypatch.setenv("BAO_TOKEN", "s.realtoken")
    monkeypatch.setattr(cli, "connect", lambda conn, cred, **k: client)

    code, text = run(["apply", "--phase", "root-config", "--jwt-discovery-url", ISSUER, "--apply"])
    assert code == 2
    assert "blocked enable-secrets-engine sys/mounts/kv" in text
    # The ops after the blocked one are still reported, and nothing was written.
    assert "create write-policy sys/policies/acl/team-readwrite" in text
    assert client.calls == []


def test_apply_with_flag_connects_and_applies(monkeypatch):
    calls = {}

    def fake_connect(conn, credential, **k):
        calls["connected"] = conn
        return "FAKE_CLIENT"

    def fake_apply_plan(plan, *, client=None, apply=False):
        calls["client"] = client
        calls["apply"] = apply
        return ApplyResult(applied=tuple(op.summary() for op in plan.ops), dry_run=False)

    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    monkeypatch.setenv("BAO_TOKEN", "s.realtoken")
    monkeypatch.setattr(cli, "connect", fake_connect)
    monkeypatch.setattr(cli, "apply_plan", fake_apply_plan)

    code, text = run(["apply", "--phase", "root-config", "--jwt-discovery-url", ISSUER, "--apply"])
    assert code == 0
    assert calls["client"] == "FAKE_CLIENT"
    assert calls["apply"] is True
    assert "Applied the following operations:" in text


def test_apply_with_flag_reports_connection_error(monkeypatch):
    def boom_connect(*a, **k):
        from ladon.apply.client import BaoConnectionError

        raise BaoConnectionError("not authenticated")

    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    monkeypatch.setenv("BAO_TOKEN", "s.realtoken")
    monkeypatch.setattr(cli, "connect", boom_connect)

    code, _ = run(["apply", "--phase", "root-config", "--jwt-discovery-url", ISSUER, "--apply"])
    assert code == 2


def test_the_namespace_travels_on_the_ops_not_on_the_connection(monkeypatch):
    # The plan says which namespace each op runs in, so one connection serves a
    # whole plan and a root op and a team op cannot be confused for each other.
    seen = {}

    def fake_connect(conn, credential, **k):
        seen["connection_namespace"] = conn.namespace
        return "C"

    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    monkeypatch.setenv("BAO_TOKEN", "s.realtoken")
    monkeypatch.setattr(cli, "connect", fake_connect)

    def record(plan, **k):
        seen["op_namespaces"] = [o.namespace for o in plan.ops]
        return ApplyResult(applied=(), dry_run=False)

    monkeypatch.setattr(cli, "apply_plan", record)

    run(
        [
            "apply",
            "--phase",
            "team-onboarding",
            "--cluster",
            "cluster-a",
            "--oidc-discovery-url",
            ISSUER,
            "--namespace",
            "team-a",
            "--apply",
        ]
    )
    assert seen["connection_namespace"] is None
    assert seen["op_namespaces"] == ["team-a"] * 4


def test_no_subcommand_errors():
    with pytest.raises(SystemExit):
        cli.main([])


def test_build_plan_unknown_phase_raises():
    import argparse

    ns = argparse.Namespace(phase="bogus", config=None)
    with pytest.raises(ConfigError):
        cli.build_plan(ns)


CONFIG_YAML = """
deployment:
  kv_mount: team-kv
teams:
  - name: team-a
    namespace: ns-a
    environments: [dev]
"""


def write_config(tmp_path):
    path = tmp_path / "deployment.yaml"
    path.write_text(CONFIG_YAML)
    return str(path)


def test_root_config_kv_mount_from_config(tmp_path):
    cfg = write_config(tmp_path)
    code, text = run(
        ["plan", "--phase", "root-config", "--jwt-discovery-url", ISSUER, "--config", cfg]
    )
    assert code == 0
    assert "enable-secrets-engine sys/mounts/team-kv" in text


def test_explicit_kv_mount_overrides_config(tmp_path):
    cfg = write_config(tmp_path)
    code, text = run(
        [
            "plan",
            "--phase",
            "root-config",
            "--jwt-discovery-url",
            ISSUER,
            "--config",
            cfg,
            "--kv-mount",
            "override-kv",
        ]
    )
    assert code == 0
    assert "sys/mounts/override-kv" in text


def test_team_onboarding_namespace_resolved_from_config(tmp_path):
    cfg = write_config(tmp_path)
    code, text = run(
        [
            "plan",
            "--phase",
            "team-onboarding",
            "--cluster",
            "cluster-a",
            "--oidc-discovery-url",
            ISSUER,
            "--config",
            cfg,
            "--team",
            "team-a",
        ]
    )
    assert code == 0
    assert "[ns-a] enable-auth-method sys/auth/jwt-cluster-a" in text


def test_team_flag_without_config_errors():
    code, _ = run(
        [
            "plan",
            "--phase",
            "team-onboarding",
            "--cluster",
            "cluster-a",
            "--oidc-discovery-url",
            ISSUER,
            "--team",
            "team-a",
        ]
    )
    assert code == 2  # --team requires --config


def test_explicit_namespace_beats_team_resolution(tmp_path):
    cfg = write_config(tmp_path)
    code, text = run(
        [
            "plan",
            "--phase",
            "team-onboarding",
            "--cluster",
            "cluster-a",
            "--oidc-discovery-url",
            ISSUER,
            "--config",
            cfg,
            "--team",
            "team-a",
            "--namespace",
            "explicit-ns",
        ]
    )
    assert code == 0
    assert "[explicit-ns] enable-auth-method" in text


# Namespaces and app onboarding -----------------------------------------------


def test_root_config_creates_the_teams_from_config(tmp_path):
    cfg = write_config(tmp_path)
    code, text = run(
        ["plan", "--phase", "root-config", "--jwt-discovery-url", ISSUER, "--config", cfg]
    )
    assert code == 0
    assert "ensure-namespace sys/namespaces/ns-a" in text
    assert "[ns-a] enable-secrets-engine sys/mounts/team-kv" in text
    assert "[ns-a] write-policy sys/policies/acl/external-secrets-read" in text


def test_root_config_without_a_config_has_no_teams():
    code, text = run(["plan", "--phase", "root-config", "--jwt-discovery-url", ISSUER])
    assert code == 0
    assert "ensure-namespace" not in text


def test_root_config_narrowed_to_one_team_touches_nothing_at_the_root(tmp_path):
    # What approving a namespace request applies. It needs no auth inputs,
    # because it plans nothing at the root that would use them.
    cfg = write_config(tmp_path)
    code, text = run(["plan", "--phase", "root-config", "--config", cfg, "--teams", "team-a"])
    assert code == 0
    assert text.strip().splitlines() == [
        "ensure-namespace sys/namespaces/ns-a",
        "[ns-a] enable-secrets-engine sys/mounts/team-kv",
        "[ns-a] write-policy sys/policies/acl/team-readwrite",
        "[ns-a] write-policy sys/policies/acl/external-secrets-read",
    ]


def test_root_config_narrowed_to_an_unknown_team_is_refused(tmp_path):
    cfg = write_config(tmp_path)
    code, _ = run(["plan", "--phase", "root-config", "--config", cfg, "--teams", "team-z"])
    assert code == 2


# The team's namespace defaults to its name here, so the rendered plan reads the
# way an operator writes the command.
APP_CONFIG_YAML = """
teams:
  - name: team-a
    environments: [dev]
"""

# The full name of the sample application's identity: team, app, and the digest
# that disambiguates the pair. See tests/test_config_apps.py.
APP_IDENTITY = "eso-team-a-sample-a84085"


def write_app_config(tmp_path):
    path = tmp_path / "apps.yaml"
    path.write_text(APP_CONFIG_YAML)
    return str(path)


def app_argv(cfg, *extra):
    return [
        "plan",
        "--phase",
        "app-onboarding",
        "--team",
        "team-a",
        "--app",
        "sample",
        "--cluster",
        "cluster-a",
        "--config",
        cfg,
        *extra,
    ]


def test_plan_app_onboarding_prints_both_halves(tmp_path, monkeypatch):
    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    monkeypatch.setattr(cli, "connect", refuse_to_connect)
    code, text = run(app_argv(write_app_config(tmp_path)))
    assert code == 0
    openbao, cluster = text.split("Cluster documents")
    assert openbao.strip().splitlines()[-2:] == [
        "[team-a] write-policy sys/policies/acl/eso-sample-readonly",
        f"[team-a] write-auth-role auth/jwt-cluster-a/role/{APP_IDENTITY}",
    ]
    assert "kind: ServiceAccount" in cluster
    assert "kind: ClusterSecretStore" in cluster
    assert "name: ladon-team-a-sample-a84085" in cluster
    # The store is the one the cluster layer applies; nothing here touches it.
    assert "capabilities" not in openbao


def test_app_onboarding_namespace_and_mount_come_from_the_config(tmp_path, monkeypatch):
    cfg = write_config(tmp_path)
    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    code, text = run(
        [
            "plan",
            "--phase",
            "app-onboarding",
            "--team",
            "team-a",
            "--app",
            "sample",
            "--cluster",
            "cluster-a",
            "--config",
            cfg,
        ]
    )
    assert code == 0
    assert "[ns-a] write-policy sys/policies/acl/eso-sample-readonly" in text
    assert "namespace: ns-a" in text
    assert "path: team-kv" in text


def test_app_onboarding_kubernetes_namespaces_are_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    code, text = run(
        app_argv(
            write_app_config(tmp_path),
            "--k8s-namespace",
            "ladon-sample",
            "--sa-namespace",
            "ladon-system",
            "--audience",
            "ladon",
        )
    )
    assert code == 0
    assert "kubernetes.io/metadata.name: ladon-sample" in text
    assert "namespace: ladon-system" in text
    assert "- ladon" in text


def test_app_onboarding_requires_an_app_and_a_team(tmp_path):
    code, _ = run(
        [
            "plan",
            "--phase",
            "app-onboarding",
            "--cluster",
            "cluster-a",
            "--config",
            write_app_config(tmp_path),
        ]
    )
    assert code == 2


def test_app_onboarding_requires_a_config(monkeypatch):
    # Guessing the namespace or the KV mount renders a policy and a role that
    # look right and grant access to a path nothing uses.
    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    code, _ = run(
        [
            "plan",
            "--phase",
            "app-onboarding",
            "--team",
            "team-a",
            "--app",
            "sample",
            "--cluster",
            "cluster-a",
        ]
    )
    assert code == 2


def test_app_onboarding_needs_a_server_for_the_store(tmp_path, monkeypatch):
    monkeypatch.delenv("BAO_ADDR", raising=False)
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    code, _ = run(app_argv(write_app_config(tmp_path)))
    assert code == 2


def test_an_address_carrying_credentials_is_refused(tmp_path, monkeypatch):
    # The address is rendered verbatim into a document that gets printed and
    # reviewed, so a password in it would be published.
    monkeypatch.delenv("BAO_ADDR", raising=False)
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    cfg = write_app_config(tmp_path)
    code, text = run(app_argv(cfg, "--server", "https://user:pw@bao.example:8200"))
    assert code == 2
    assert "pw" not in text

    monkeypatch.setenv("BAO_ADDR", "https://user:pw@bao.example:8200")
    code, text = run(app_argv(cfg))
    assert code == 2
    assert "pw" not in text


def test_a_blocked_openbao_half_keeps_the_cluster_half_from_being_applied(tmp_path, monkeypatch):
    # A store applied against a role that was never written fails at refresh
    # time, somewhere else, later.
    blocked = OpDiff(summary="write-auth-role auth/jwt-cluster-a/role/x", action="blocked")
    cluster = FakeCluster()
    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    monkeypatch.setenv("BAO_TOKEN", "s.realtoken")
    monkeypatch.setattr(cli, "connect", lambda conn, cred, **k: "C")
    monkeypatch.setattr(
        cli,
        "apply_plan",
        lambda plan, **k: ApplyResult(applied=(), dry_run=False, diffs=(blocked,)),
    )
    use_cluster(monkeypatch, cluster)

    code, text = run(["apply", *app_argv(write_app_config(tmp_path))[1:], "--apply"])
    assert code == 2
    assert "the OpenBao half did not land" in text
    assert cluster.calls == []


# The cluster half -------------------------------------------------------------

# An operator already running on the cluster, which is what Ladon reuses (D18a).
ESO_RUNNING = ESOInstall(
    namespace="external-secrets", service_account="external-secrets", version="v0.19.2"
)

# The store the app-onboarding phase renders and the sync phase points at.
APP_STORE = "ladon-team-a-sample-a84085"


def use_cluster(monkeypatch, cluster, *, api_version="v1"):
    """Point the CLI's cluster half at a fake cluster with an operator on it."""
    monkeypatch.setattr(cli, "detect_eso", lambda: ESO_RUNNING)
    monkeypatch.setattr(cli, "eso_api_version", lambda: api_version)
    monkeypatch.setattr(
        cli,
        "converge_documents",
        lambda docs, **kwargs: converge_documents(docs, run=cluster, **kwargs),
    )


def cluster_lines(text):
    return [line.strip() for line in text.splitlines() if line.strip().startswith("[cluster]")]


def with_bao(monkeypatch):
    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    monkeypatch.setenv("BAO_TOKEN", "s.realtoken")


def test_apply_app_onboarding_applies_both_halves_and_converges(tmp_path, monkeypatch):
    client = FakeOpenBao()
    cluster = FakeCluster()
    with_bao(monkeypatch)
    monkeypatch.setattr(cli, "connect", lambda conn, cred, **k: client)
    use_cluster(monkeypatch, cluster)

    argv = ["apply", *app_argv(write_app_config(tmp_path))[1:], "--apply"]
    code, first = run(argv)
    assert code == 0
    assert "eso-sample-readonly" in client.state("team-a").policies
    assert cluster_lines(first) == [
        f"[cluster] create ServiceAccount/ladon/{APP_IDENTITY}",
        f"[cluster] create ClusterSecretStore/{APP_STORE}",
    ]

    writes = len(cluster.applies)
    code, second = run(argv)
    assert code == 0
    assert cluster_lines(second) == [
        f"[cluster] already current ServiceAccount/ladon/{APP_IDENTITY}",
        f"[cluster] already current ClusterSecretStore/{APP_STORE}",
    ]
    # A second run writes nothing, to OpenBao or to the cluster.
    assert len(cluster.applies) == writes
    assert "Nothing to apply" in second


def test_app_onboarding_dry_run_diffs_both_halves_and_writes_nothing(tmp_path, monkeypatch):
    client = FakeOpenBao()
    cluster = FakeCluster()
    with_bao(monkeypatch)
    monkeypatch.setattr(cli, "connect", lambda conn, cred, **k: client)
    use_cluster(monkeypatch, cluster)

    code, text = run(["apply", *app_argv(write_app_config(tmp_path))[1:]])
    assert code == 0
    assert "DRY RUN" in text
    assert cluster_lines(text) == [
        f"[cluster] create ServiceAccount/ladon/{APP_IDENTITY}",
        f"[cluster] create ClusterSecretStore/{APP_STORE}",
    ]
    assert "--dry-run=server" in cluster.applies[0]
    assert cluster.objects == {}
    assert client.calls == []
    assert "Re-run with --apply" in text


def test_app_onboarding_refuses_a_cluster_with_no_operator(tmp_path, monkeypatch):
    # Before any write: a store applied to a cluster with no operator is a
    # document nothing reads, and the OpenBao half would already be written.
    client = FakeOpenBao()
    with_bao(monkeypatch)
    monkeypatch.setattr(cli, "connect", lambda conn, cred, **k: client)
    monkeypatch.setattr(cli, "detect_eso", lambda: None)

    code, _ = run(["apply", *app_argv(write_app_config(tmp_path))[1:], "--apply"])
    assert code == 2
    assert client.calls == []


def test_the_store_is_rendered_at_the_api_version_the_cluster_stores(tmp_path, monkeypatch):
    client = FakeOpenBao()
    cluster = FakeCluster()
    with_bao(monkeypatch)
    monkeypatch.setattr(cli, "connect", lambda conn, cred, **k: client)
    use_cluster(monkeypatch, cluster, api_version="v1beta1")

    code, _ = run(["apply", *app_argv(write_app_config(tmp_path))[1:], "--apply"])
    assert code == 0
    applied = list(yaml.safe_load_all(cluster.streams[-1]))
    assert applied[-1]["apiVersion"] == "external-secrets.io/v1beta1"


def test_plan_stays_offline_and_renders_the_default_api_version(tmp_path, monkeypatch):
    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    monkeypatch.setattr(cli, "detect_eso", refuse_to_connect)
    code, text = run(app_argv(write_app_config(tmp_path)))
    assert code == 0
    assert "apiVersion: external-secrets.io/v1" in text


def test_a_blocked_cluster_document_exits_non_zero(tmp_path, monkeypatch):
    client = FakeOpenBao()
    cluster = FakeCluster(blocked=[APP_STORE])
    with_bao(monkeypatch)
    monkeypatch.setattr(cli, "connect", lambda conn, cred, **k: client)
    use_cluster(monkeypatch, cluster)

    code, text = run(["apply", *app_argv(write_app_config(tmp_path))[1:], "--apply"])
    assert code == 2
    assert f"[cluster] blocked ClusterSecretStore/{APP_STORE}" in text
    assert "conflict" in text


def test_a_cluster_that_did_not_store_what_was_applied_is_an_error(tmp_path, monkeypatch):
    client = FakeOpenBao()
    cluster = FakeCluster(mutate=lambda doc: {**doc, "spec": {}} if "spec" in doc else doc)
    with_bao(monkeypatch)
    monkeypatch.setattr(cli, "connect", lambda conn, cred, **k: client)
    use_cluster(monkeypatch, cluster)

    code, text = run(["apply", *app_argv(write_app_config(tmp_path))[1:], "--apply"])
    assert code == 2
    # The documents were written, so the report is printed before the failure:
    # an operator has to know what landed.
    assert cluster_lines(text) == [
        f"[cluster] create ServiceAccount/ladon/{APP_IDENTITY}",
        f"[cluster] create ClusterSecretStore/{APP_STORE}",
    ]


def test_a_cluster_half_that_is_wholly_blocked_does_not_claim_a_write(tmp_path, monkeypatch):
    client = FakeOpenBao()
    cluster = FakeCluster(blocked=[APP_IDENTITY, APP_STORE])
    with_bao(monkeypatch)
    monkeypatch.setattr(cli, "connect", lambda conn, cred, **k: client)
    use_cluster(monkeypatch, cluster)

    code, text = run(["apply", *app_argv(write_app_config(tmp_path))[1:], "--apply"])
    assert code == 2
    assert "Cluster documents (nothing applied):" in text
    assert [line.split(" ")[1] for line in cluster_lines(text)] == ["blocked", "blocked"]


# The sync phase ---------------------------------------------------------------


def sync_argv(*extra):
    return [
        "--phase",
        "sync",
        "--team",
        "team-a",
        "--app",
        "sample",
        "--path",
        "apps/sample/demo",
        "--k8s-namespace",
        "ladon-sample",
        "--name",
        "sample-demo",
        *extra,
    ]


def test_plan_sync_renders_an_external_secret_pointing_at_the_app_store():
    code, text = run(["plan", *sync_argv()])
    assert code == 0
    assert "kind: ExternalSecret" in text
    assert f"name: {APP_STORE}" in text
    assert "key: apps/sample/demo" in text
    assert "namespace: ladon-sample" in text


def test_apply_sync_converges_the_external_secret(monkeypatch):
    cluster = FakeCluster()
    # The sync phase has no OpenBao half, so it needs no address and no token.
    monkeypatch.delenv("BAO_ADDR", raising=False)
    monkeypatch.delenv("BAO_TOKEN", raising=False)
    monkeypatch.setattr(cli, "connect", refuse_to_connect)
    use_cluster(monkeypatch, cluster)

    note = "(apps/sample/demo -> Secret/sample-demo)"
    code, first = run(["apply", *sync_argv(), "--apply"])
    assert code == 0
    assert cluster_lines(first) == [
        f"[cluster] create ExternalSecret/ladon-sample/sample-demo {note}"
    ]

    code, second = run(["apply", *sync_argv(), "--apply"])
    assert code == 0
    assert cluster_lines(second) == [
        f"[cluster] already current ExternalSecret/ladon-sample/sample-demo {note}"
    ]
    assert len(cluster.applies) == 1


def test_sync_targets_the_secret_name_the_consumer_already_uses(monkeypatch):
    cluster = FakeCluster()
    use_cluster(monkeypatch, cluster)
    code, text = run(["plan", *sync_argv("--target-name", "sample-credentials")])
    assert code == 0
    assert "name: sample-credentials" in text


def test_sync_refuses_a_path_outside_the_application_prefix(monkeypatch):
    # The application's policy grants read on its own prefix and nothing else,
    # so ESO would be refused at refresh time, somewhere else, later.
    cluster = FakeCluster()
    use_cluster(monkeypatch, cluster)
    code, _ = run(["apply", *sync_argv()[:6], "--path", "apps/other/demo", *sync_argv()[8:]])
    assert code == 2


def test_sync_requires_the_parts_of_the_name_it_cannot_guess():
    code, _ = run(["plan", "--phase", "sync", "--team", "team-a", "--app", "sample"])
    assert code == 2


# install-eso ------------------------------------------------------------------


def fake_installer(seen, **result_fields):
    def install(release, *, apply):
        seen["namespace"] = release.namespace
        seen["apply"] = apply
        return ESOInstallResult(**result_fields)

    return install


def test_install_eso_reports_an_operator_it_reused(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli,
        "install_eso",
        fake_installer(
            seen,
            summary="reusing external-secrets v0.19.2 in namespace external-secrets",
            reused=True,
            existing=ESO_RUNNING,
            api_version="v1",
        ),
    )
    code, text = run(["install-eso"])
    assert code == 0
    assert "reusing external-secrets v0.19.2" in text
    assert "v1" in text
    assert seen["apply"] is False


def test_install_eso_is_a_dry_run_unless_applied(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli,
        "install_eso",
        fake_installer(seen, summary="dry run: would install external-secrets", reused=False),
    )
    code, text = run(["install-eso"])
    assert code == 0
    assert "Re-run with --apply" in text


def test_install_eso_installs_into_the_namespace_it_is_given(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli,
        "install_eso",
        fake_installer(seen, summary="installed external-secrets", reused=False),
    )
    code, text = run(["install-eso", "--namespace", "ns-a", "--apply"])
    assert code == 0
    assert seen == {"namespace": "ns-a", "apply": True}
    assert "Re-run with --apply" not in text


def test_install_eso_reports_a_cluster_failure_without_a_traceback(monkeypatch):
    def boom(release, *, apply):
        raise K8sError("kubectl 'not found on PATH'")

    monkeypatch.setattr(cli, "install_eso", boom)
    code, _ = run(["install-eso"])
    assert code == 2


def test_a_converged_cluster_half_says_so_rather_than_claiming_a_write(tmp_path, monkeypatch):
    client = FakeOpenBao()
    cluster = FakeCluster()
    with_bao(monkeypatch)
    monkeypatch.setattr(cli, "connect", lambda conn, cred, **k: client)
    use_cluster(monkeypatch, cluster)

    argv = ["apply", *app_argv(write_app_config(tmp_path))[1:], "--apply"]
    run(argv)
    _, second = run(argv)
    assert "Cluster documents: every document is already current." in second
    assert "(applied)" not in second


def test_sync_names_the_path_and_the_target_secret_before_applying(monkeypatch):
    # Secrets hygiene: a dry-run shows the path and the sync target, never a
    # value, so what is about to be wired is reviewable.
    cluster = FakeCluster()
    use_cluster(monkeypatch, cluster)
    code, text = run(["apply", *sync_argv("--target-name", "sample-credentials")])
    assert code == 0
    assert cluster_lines(text) == [
        "[cluster] create ExternalSecret/ladon-sample/sample-demo "
        "(apps/sample/demo -> Secret/sample-credentials)"
    ]


def test_sync_defaults_the_namespace_to_the_application(monkeypatch):
    cluster = FakeCluster()
    use_cluster(monkeypatch, cluster)
    argv = [
        "plan",
        "--phase",
        "sync",
        "--team",
        "team-a",
        "--app",
        "sample",
        "--path",
        "apps/sample/demo",
        "--name",
        "sample-demo",
    ]
    code, text = run(argv)
    assert code == 0
    assert "namespace: sample" in text


def test_sync_refuses_a_path_that_climbs_out_of_the_prefix(monkeypatch):
    cluster = FakeCluster()
    use_cluster(monkeypatch, cluster)
    code, _ = run(["apply", *sync_argv()[:6], "--path", "apps/sample/../other", *sync_argv()[8:]])
    assert code == 2
    assert cluster.calls == []


# The seam the backend shares ---------------------------------------------------


def phase_args(**fields):
    """The flags one phase runs with, as the backend's job runner builds them."""
    defaults = {
        "phase": "app-onboarding",
        "config": None,
        "namespace": None,
        "kv_mount": None,
        "team": "team-a",
        "teams": None,
        "app": "sample",
        "cluster": "cluster-a",
        "k8s_namespace": None,
        "sa_namespace": "ladon",
        "audience": "bao",
        "server": "https://bao.example:8200",
        "jwt_discovery_url": None,
        "oidc_discovery_url": None,
        "oidc_client_id": None,
        "oidc_client_secret_ref": None,
    }
    return argparse.Namespace(**{**defaults, **fields})


def test_run_phase_takes_a_loaded_config_and_an_address_and_reports_both_halves(
    tmp_path, monkeypatch
):
    # The backend never takes a config path from a request: it hands the
    # deployment's own loaded config in, and the address it was configured with.
    client = FakeOpenBao()
    cluster = FakeCluster()
    dialled = {}
    monkeypatch.setenv("BAO_TOKEN", "s.realtoken")
    monkeypatch.delenv("BAO_ADDR", raising=False)

    def record_and_connect(conn, credential, **kwargs):
        dialled["address"] = conn.address
        return client

    monkeypatch.setattr(cli, "connect", record_and_connect)
    use_cluster(monkeypatch, cluster)
    config = load_deployment_config(write_app_config(tmp_path))

    run = cli.run_phase(phase_args(), config=config, apply=True, address="https://vault.example")

    assert dialled["address"] == "https://vault.example"
    assert [d.action for d in run.openbao.diffs] == ["create", "create"]
    assert [r.action for r in run.cluster.results] == ["create", "create"]
    assert run.blocked is False
    assert run.error is None

    # A second run converges: nothing is written to either half.
    again = cli.run_phase(phase_args(), config=config, apply=True, address="https://vault.example")
    assert [d.action for d in again.openbao.diffs] == ["already current"] * 2
    assert [r.action for r in again.cluster.results] == ["already current"] * 2


def test_run_phase_keeps_the_cluster_half_from_a_blocked_openbao_half(tmp_path, monkeypatch):
    blocked = OpDiff(summary="write-auth-role auth/jwt-cluster-a/role/x", action="blocked")
    cluster = FakeCluster()
    monkeypatch.setenv("BAO_TOKEN", "s.realtoken")
    monkeypatch.setattr(cli, "connect", lambda conn, cred, **k: "C")
    monkeypatch.setattr(
        cli,
        "apply_plan",
        lambda plan, **k: ApplyResult(applied=(), dry_run=False, diffs=(blocked,)),
    )
    use_cluster(monkeypatch, cluster)
    config = load_deployment_config(write_app_config(tmp_path))

    run = cli.run_phase(phase_args(), config=config, apply=True, address="https://vault.example")

    assert run.blocked is True
    assert run.cluster is None
    assert cluster.calls == []


def test_run_phase_reports_a_cluster_that_did_not_store_what_was_applied(tmp_path, monkeypatch):
    # The documents were written, so the report travels with the failure
    # rather than being raised out from under it.
    client = FakeOpenBao()
    cluster = FakeCluster(mutate=lambda doc: {**doc, "spec": {}} if "spec" in doc else doc)
    with_bao(monkeypatch)
    monkeypatch.setattr(cli, "connect", lambda conn, cred, **k: client)
    use_cluster(monkeypatch, cluster)
    config = load_deployment_config(write_app_config(tmp_path))

    run = cli.run_phase(phase_args(), config=config, apply=True)

    assert run.error and "did not store" in run.error
    assert [r.action for r in run.cluster.results] == ["create", "create"]


# seal-values: the overlay that carries the auto-unseal stanza. The rendering
# itself is tested in test_config_seal_values.py; these check the CLI contract,
# which is the exit code and a stdout that is nothing but the overlay.

KMS_CONFIG_YAML = """
deployment:
  kms:
    provider: gcpckms
    project: example-project
    region: example-location1
    key_ring: example-ring
    crypto_key: example-key
"""

SHAMIR_CONFIG_YAML = """
deployment:
  kms:
    provider: shamir
"""

BASE_VALUES_YAML = """
server:
  ha:
    raft:
      config: |
        ui = true

        storage "raft" {
          path = "/openbao/data"
        }

        service_registration "kubernetes" {}
"""

BASE_HCL_TEXT = """ui = true

storage "raft" {
  path = "/openbao/data"
}

service_registration "kubernetes" {}
"""


def write_kms_files(
    tmp_path, config=KMS_CONFIG_YAML, base=BASE_VALUES_YAML, base_name="values.yaml"
):
    config_path = tmp_path / "deployment.yaml"
    config_path.write_text(config)
    base_path = tmp_path / base_name
    base_path.write_text(base)
    return str(config_path), str(base_path)


def test_seal_values_extends_a_values_file(tmp_path):
    config, base = write_kms_files(tmp_path)
    code, text = run(["seal-values", "--config", config, "--base-config", base])
    assert code == 0
    document = yaml.safe_load(text)
    hcl = document["server"]["ha"]["raft"]["config"]
    assert 'seal "gcpckms" {' in hcl
    assert 'crypto_key = "example-key"' in hcl
    assert "serviceAccount" not in document["server"]


def test_seal_values_extends_a_plain_hcl_file(tmp_path):
    config, base = write_kms_files(tmp_path, base=BASE_HCL_TEXT, base_name="server.hcl")
    code, text = run(["seal-values", "--config", config, "--base-config", base])
    assert code == 0
    assert 'seal "gcpckms" {' in yaml.safe_load(text)["server"]["ha"]["raft"]["config"]


def test_seal_values_adds_service_account_annotations(tmp_path):
    config, base = write_kms_files(tmp_path)
    code, text = run(
        [
            "seal-values",
            "--config",
            config,
            "--base-config",
            base,
            "--service-account-annotation",
            "iam.gke.io/gcp-service-account=unseal@example.example.com",
            "--service-account-annotation",
            "example.dev/owner=platform",
        ]
    )
    assert code == 0
    annotations = yaml.safe_load(text)["server"]["serviceAccount"]["annotations"]
    assert annotations == {
        "iam.gke.io/gcp-service-account": "unseal@example.example.com",
        "example.dev/owner": "platform",
    }


def test_seal_values_refuses_shamir_and_prints_nothing(tmp_path):
    config, base = write_kms_files(tmp_path, config=SHAMIR_CONFIG_YAML)
    code, text = run(["seal-values", "--config", config, "--base-config", base])
    assert code == 2
    assert text == ""


def test_seal_values_refuses_a_config_with_no_kms_block(tmp_path):
    config, base = write_kms_files(tmp_path, config="deployment:\n  kv_mount: kv\n")
    code, text = run(["seal-values", "--config", config, "--base-config", base])
    assert code == 2
    assert text == ""


def test_seal_values_refuses_a_values_file_with_no_server_config(tmp_path):
    config, base = write_kms_files(tmp_path, base="server:\n  ha:\n    replicas: 1\n")
    code, text = run(["seal-values", "--config", config, "--base-config", base])
    assert code == 2
    assert text == ""


def test_seal_values_refuses_a_malformed_annotation(tmp_path):
    config, base = write_kms_files(tmp_path)
    code, text = run(
        [
            "seal-values",
            "--config",
            config,
            "--base-config",
            base,
            "--service-account-annotation",
            "no-equals-sign",
        ]
    )
    assert code == 2
    assert text == ""


def test_seal_values_refuses_a_missing_base_config(tmp_path):
    config, _ = write_kms_files(tmp_path)
    code, text = run(
        ["seal-values", "--config", config, "--base-config", str(tmp_path / "nope.yaml")]
    )
    assert code == 2
    assert text == ""
