"""Tests for the ESO install command construction and dry-run safety (D4a).

Pure argv builders need no kubectl/helm. The execution path is exercised with
``run`` monkeypatched, so nothing shells out to a real cluster.
"""

import pytest

from ladon.install import eso
from ladon.install.eso import (
    ESO_CHART,
    ESO_CHART_VERSION,
    ESO_WEBHOOK_DEPLOYMENT,
    ESORelease,
    install_eso,
)
from ladon.install.helm import HelmError


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
    monkeypatch.setattr(eso, "run", lambda argv: calls.append(argv) or _ok(argv))
    install_eso()
    # repo add, repo update, upgrade (dry-run). No webhook wait on a dry-run.
    assert len(calls) == 3
    assert "--dry-run" in calls[2]
    assert not any(c[0] == "kubectl" for c in calls)


def test_apply_runs_install_then_waits_for_webhook(monkeypatch):
    calls = []
    monkeypatch.setattr(eso, "run", lambda argv: calls.append(argv) or _ok(argv))
    install_eso(apply=True)
    assert len(calls) == 4
    assert "--dry-run" not in calls[2]
    assert calls[3][0] == "kubectl"
    assert "--for=condition=Available" in calls[3]


def test_apply_can_skip_webhook_wait(monkeypatch):
    calls = []
    monkeypatch.setattr(eso, "run", lambda argv: calls.append(argv) or _ok(argv))
    install_eso(apply=True, wait_for_webhook=False)
    assert len(calls) == 3
    assert not any(c[0] == "kubectl" for c in calls)


def test_webhook_failure_is_reframed(monkeypatch):
    def fake_run(argv):
        if argv[0] == "kubectl":
            raise HelmError("timed out waiting for the condition")
        return _ok(argv)

    monkeypatch.setattr(eso, "run", fake_run)
    with pytest.raises(HelmError, match="webhook did not become ready"):
        install_eso(apply=True)


class _Result:
    def __init__(self, argv):
        self.args = argv
        self.returncode = 0
        self.stdout = ""
        self.stderr = ""


def _ok(argv):
    return _Result(argv)
