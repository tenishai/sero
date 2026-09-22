"""Tests for the apply CLI entrypoint (D3b).

The ``plan`` path is pure (no connection). The ``apply`` path connects in both
modes (a dry-run reads live state to diff against it), so its connect and
execute calls are monkeypatched, or pointed at the recording fake, and nothing
touches a live OpenBao.
"""

import io

import pytest

from fake_openbao import FakeOpenBao
from ladon.apply import cli
from ladon.apply.executor import ApplyResult, OpDiff
from ladon.config.teams import ConfigError

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


def test_apply_app_onboarding_applies_the_openbao_half_only(tmp_path, monkeypatch):
    client = FakeOpenBao()
    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    monkeypatch.setenv("BAO_TOKEN", "s.realtoken")
    monkeypatch.setattr(cli, "connect", lambda conn, cred, **k: client)

    code, text = run(["apply", *app_argv(write_app_config(tmp_path))[1:], "--apply"])
    assert code == 0
    assert "eso-sample-readonly" in client.state("team-a").policies
    assert "kind: ClusterSecretStore" in text
    # Applying the cluster half is a later step; the documents are printed.
    assert "not applied" in text


def test_a_blocked_apply_marks_the_documents_as_not_to_be_applied(tmp_path, monkeypatch):
    # A store applied against a role that was never written fails at refresh
    # time, somewhere else, later.
    blocked = OpDiff(summary="write-auth-role auth/jwt-cluster-a/role/x", action="blocked")
    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    monkeypatch.setenv("BAO_TOKEN", "s.realtoken")
    monkeypatch.setattr(cli, "connect", lambda conn, cred, **k: "C")
    monkeypatch.setattr(
        cli,
        "apply_plan",
        lambda plan, **k: ApplyResult(applied=(), dry_run=False, diffs=(blocked,)),
    )

    code, text = run(["apply", *app_argv(write_app_config(tmp_path))[1:], "--apply"])
    assert code == 2
    assert "DO NOT APPLY" in text
    assert "the OpenBao half was not applied" in text
