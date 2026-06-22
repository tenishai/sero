"""Tests for the apply CLI entrypoint (D3b).

The ``plan`` path is pure (no connection). The ``apply`` path's connect and
execute calls are monkeypatched so nothing touches a live OpenBao.
"""

import io

import pytest

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


def test_apply_without_flag_is_a_dry_run_and_does_not_connect(monkeypatch):
    # connect must never be called on a dry-run.
    def fail_connect(*a, **k):
        raise AssertionError("connect should not be called on a dry-run")

    monkeypatch.setattr(cli, "connect", fail_connect)
    code, text = run(["apply", "--phase", "root-config", "--jwt-discovery-url", ISSUER])
    assert code == 0
    assert "DRY RUN" in text
    assert "Re-run with --apply" in text
    assert "enable-secrets-engine sys/mounts/kv" in text


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

    ns = argparse.Namespace(phase="bogus")
    with pytest.raises(ConfigError):
        cli.build_plan(ns)
