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
from ladon.apply.executor import ApplyResult
from ladon.config.teams import ConfigError

ISSUER = "https://issuer.example/.well-known/openid-configuration"


def run(argv):
    out = io.StringIO()
    code = cli.main(argv, out=out)
    return code, out.getvalue()


def test_plan_root_config_renders_without_connecting():
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


def test_plan_team_onboarding_renders():
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


def test_apply_passes_namespace_to_connection(monkeypatch):
    seen = {}

    def fake_connect(conn, credential, **k):
        seen["namespace"] = conn.namespace
        return "C"

    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    monkeypatch.setenv("BAO_TOKEN", "s.realtoken")
    monkeypatch.setattr(cli, "connect", fake_connect)
    monkeypatch.setattr(cli, "apply_plan", lambda plan, **k: ApplyResult(applied=(), dry_run=False))

    run(
        [
            "apply",
            "--phase",
            "team-onboarding",
            "--cluster",
            "team-a",
            "--oidc-discovery-url",
            ISSUER,
            "--namespace",
            "team-a",
            "--apply",
        ]
    )
    assert seen["namespace"] == "team-a"


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


def test_team_onboarding_namespace_resolved_from_config(tmp_path, monkeypatch):
    cfg = write_config(tmp_path)
    seen = {}

    def fake_connect(conn, credential, **k):
        seen["namespace"] = conn.namespace
        return "C"

    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    monkeypatch.setenv("BAO_TOKEN", "s.realtoken")
    monkeypatch.setattr(cli, "connect", fake_connect)
    monkeypatch.setattr(cli, "apply_plan", lambda plan, **k: ApplyResult(applied=(), dry_run=False))

    code, _ = run(
        [
            "apply",
            "--phase",
            "team-onboarding",
            "--cluster",
            "team-a",
            "--oidc-discovery-url",
            ISSUER,
            "--config",
            cfg,
            "--team",
            "team-a",
            "--apply",
        ]
    )
    assert code == 0
    assert seen["namespace"] == "ns-a"


def test_team_flag_without_config_errors(monkeypatch):
    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    monkeypatch.setenv("BAO_TOKEN", "s.realtoken")
    code, _ = run(
        [
            "apply",
            "--phase",
            "team-onboarding",
            "--cluster",
            "team-a",
            "--oidc-discovery-url",
            ISSUER,
            "--team",
            "team-a",
            "--apply",
        ]
    )
    assert code == 2  # --team requires --config


def test_explicit_namespace_beats_team_resolution(tmp_path, monkeypatch):
    cfg = write_config(tmp_path)
    seen = {}
    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    monkeypatch.setenv("BAO_TOKEN", "s.realtoken")
    monkeypatch.setattr(
        cli, "connect", lambda conn, cred, **k: seen.setdefault("ns", conn.namespace) or "C"
    )
    monkeypatch.setattr(cli, "apply_plan", lambda plan, **k: ApplyResult(applied=(), dry_run=False))
    run(
        [
            "apply",
            "--phase",
            "team-onboarding",
            "--cluster",
            "team-a",
            "--oidc-discovery-url",
            ISSUER,
            "--config",
            cfg,
            "--team",
            "team-a",
            "--namespace",
            "explicit-ns",
            "--apply",
        ]
    )
    assert seen["ns"] == "explicit-ns"
