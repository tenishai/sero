"""Tests for the OpenBao install command construction (D3a).

These cover the pure argv builders and the dry-run-by-default safety, with no
helm binary required.
"""

from pathlib import Path

import pytest

from ladon.install import HelmError, HelmRelease, install, run
from ladon.install.helm import OPENBAO_CHART, OPENBAO_CHART_VERSION


def make_release():
    return HelmRelease(
        name="ladon-openbao",
        namespace="openbao",
        values_files=[
            Path("deploy/openbao/values/values-base.yaml"),
            Path("deploy/openbao/values/values-dev.yaml"),
        ],
    )


def test_upgrade_pins_chart_version():
    argv = make_release().upgrade_argv()
    assert "--version" in argv
    assert argv[argv.index("--version") + 1] == OPENBAO_CHART_VERSION
    assert OPENBAO_CHART in argv


def test_upgrade_is_dry_run_by_default():
    assert "--dry-run" in make_release().upgrade_argv()


def test_upgrade_apply_omits_dry_run():
    assert "--dry-run" not in make_release().upgrade_argv(dry_run=False)


def test_upgrade_includes_values_in_order():
    argv = make_release().upgrade_argv()
    # Each values file is preceded by -f, base before dev.
    f_positions = [i for i, a in enumerate(argv) if a == "-f"]
    files = [argv[i + 1] for i in f_positions]
    assert files == [
        "deploy/openbao/values/values-base.yaml",
        "deploy/openbao/values/values-dev.yaml",
    ]


def test_upgrade_creates_namespace_by_default():
    argv = make_release().upgrade_argv()
    assert "--create-namespace" in argv
    assert argv[argv.index("--namespace") + 1] == "openbao"


def test_repo_argv():
    r = make_release()
    assert r.repo_add_argv()[:3] == ["helm", "repo", "add"]
    assert r.repo_update_argv()[:3] == ["helm", "repo", "update"]


def test_run_raises_when_helm_missing():
    with pytest.raises(HelmError, match="not found on PATH"):
        run(["definitely-not-a-real-binary-xyz", "version"])


def test_install_defaults_to_dry_run(monkeypatch):
    calls = []

    def fake_run(argv):
        calls.append(list(argv))
        return None

    monkeypatch.setattr("ladon.install.helm.run", fake_run)
    install(make_release())
    # The third call is the upgrade; it must be a dry-run by default.
    assert "--dry-run" in calls[-1]


def test_install_apply_runs_real_upgrade(monkeypatch):
    calls = []
    monkeypatch.setattr("ladon.install.helm.run", lambda argv: calls.append(list(argv)))
    install(make_release(), apply=True)
    assert "--dry-run" not in calls[-1]
    assert "upgrade" in calls[-1]
