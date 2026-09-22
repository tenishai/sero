"""Tests for the ESO install command construction and dry-run safety (D4a, D18a).

Pure argv builders need no kubectl/helm. The execution path is exercised with
``run`` monkeypatched and the operator detection injected, so nothing shells out
to a real cluster.
"""

import pytest

from ladon.install import eso, helm
from ladon.install.eso import (
    ESO_CHART,
    ESO_CHART_VERSION,
    ESO_WEBHOOK_DEPLOYMENT,
    ESORelease,
    install_eso,
)
from ladon.install.helm import HelmError
from ladon.install.k8s import ESOInstall


def patch_run(monkeypatch, fn):
    """Patch the execution seam wherever it is reached.

    The install flow runs through ``helm.run`` (via the shared ``install``); the
    webhook wait runs through the ``run`` imported into the eso module. Patch both
    so nothing shells out to a real cluster.
    """
    monkeypatch.setattr(helm, "run", fn)
    monkeypatch.setattr(eso, "run", fn)


def no_operator():
    """Detection for a cluster with no External Secrets Operator on it."""
    return None


def existing_operator():
    return ESOInstall(namespace="ns-a", service_account="external-secrets", version="v0.19.2")


def test_upgrade_argv_dry_run_by_default():
    argv = ESORelease().upgrade_argv()
    assert argv[:5] == ["helm", "upgrade", "--install", "external-secrets", ESO_CHART]
    assert "--version" in argv and ESO_CHART_VERSION in argv
    assert "--dry-run" in argv


def test_upgrade_argv_apply_drops_dry_run():
    argv = ESORelease().upgrade_argv(dry_run=False)
    assert "--dry-run" not in argv


def test_upgrade_argv_sets_install_crds():
    argv = ESORelease().upgrade_argv()
    i = argv.index("--set")
    assert argv[i + 1] == "installCRDs=true"


def test_install_crds_can_be_disabled():
    argv = ESORelease(install_crds=False).upgrade_argv()
    assert "installCRDs=false" in argv


def test_version_is_pinned_explicitly():
    argv = ESORelease().upgrade_argv()
    assert argv[argv.index("--version") + 1] == ESO_CHART_VERSION


def test_webhook_wait_argv():
    argv = ESORelease().webhook_wait_argv()
    assert argv[0] == "kubectl"
    assert "--for=condition=Available" in argv
    assert f"deployment/{ESO_WEBHOOK_DEPLOYMENT}" in argv
    assert any(a.startswith("--timeout=") for a in argv)


def test_install_defaults_to_dry_run_and_skips_webhook(monkeypatch):
    calls = []
    patch_run(monkeypatch, lambda argv: calls.append(argv) or _ok(argv))
    result = install_eso(detect=no_operator)
    # repo add, repo update, upgrade (dry-run). No webhook wait on a dry-run.
    assert len(calls) == 3
    assert "--dry-run" in calls[2]
    assert not any(c[0] == "kubectl" for c in calls)
    assert result.reused is False
    assert result.summary.startswith("dry run: would install external-secrets")


def test_apply_runs_install_then_waits_for_webhook(monkeypatch):
    calls = []
    patch_run(monkeypatch, lambda argv: calls.append(argv) or _ok(argv))
    result = install_eso(apply=True, detect=no_operator)
    assert len(calls) == 4
    assert "--dry-run" not in calls[2]
    assert calls[3][0] == "kubectl"
    assert "--for=condition=Available" in calls[3]
    assert result.summary == (
        f"installed external-secrets chart {ESO_CHART_VERSION} in namespace external-secrets"
    )
    assert len(result.commands) == 4


def test_apply_can_skip_webhook_wait(monkeypatch):
    calls = []
    patch_run(monkeypatch, lambda argv: calls.append(argv) or _ok(argv))
    install_eso(apply=True, wait_for_webhook=False, detect=no_operator)
    assert len(calls) == 3
    assert not any(c[0] == "kubectl" for c in calls)


def test_webhook_failure_is_reframed(monkeypatch):
    def fake_run(argv):
        if argv[0] == "kubectl":
            raise HelmError("timed out waiting for the condition")
        return _ok(argv)

    patch_run(monkeypatch, fake_run)
    with pytest.raises(HelmError, match="webhook did not become ready"):
        install_eso(apply=True, detect=no_operator)


# Reusing an operator somebody else installed (D18a) --------------------------


def test_an_existing_operator_is_reused_and_no_helm_command_runs(monkeypatch):
    calls = []
    patch_run(monkeypatch, lambda argv: calls.append(argv) or _ok(argv))
    result = install_eso(apply=True, detect=existing_operator)
    assert calls == []
    assert result.reused is True
    assert result.commands == ()
    assert result.summary == "reusing external-secrets v0.19.2 in namespace ns-a"
    assert result.existing == existing_operator()


def test_reuse_is_reported_on_a_dry_run_too(monkeypatch):
    calls = []
    patch_run(monkeypatch, lambda argv: calls.append(argv) or _ok(argv))
    result = install_eso(detect=existing_operator)
    assert calls == []
    assert result.reused is True


def test_detection_defaults_to_the_cluster_probe():
    # The default is the real kubectl probe; the tests inject their own.
    import inspect

    from ladon.install.k8s import detect_eso

    assert inspect.signature(install_eso).parameters["detect"].default is detect_eso


class _Result:
    def __init__(self, argv):
        self.args = argv
        self.returncode = 0
        self.stdout = ""
        self.stderr = ""


def _ok(argv):
    return _Result(argv)
