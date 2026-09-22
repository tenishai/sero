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
from ladon.install.k8s import ESOInstall, ESONotInstalledError, K8sError


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


def no_crds():
    """The CRD probe for a cluster the operator has never been installed on."""
    raise ESONotInstalledError("the External Secrets CRDs are not installed on this cluster")


def crds_at_v1():
    return "v1"


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
    result = install_eso(detect=no_operator, probe=no_crds)
    # repo add, repo update, upgrade (dry-run). No webhook wait on a dry-run.
    assert len(calls) == 3
    assert "--dry-run" in calls[2]
    assert not any(c[0] == "kubectl" for c in calls)
    assert result.reused is False
    assert result.summary.startswith("dry run: would install external-secrets")


def test_apply_runs_install_then_waits_for_webhook(monkeypatch):
    calls = []
    patch_run(monkeypatch, lambda argv: calls.append(argv) or _ok(argv))
    result = install_eso(apply=True, detect=no_operator, probe=no_crds)
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
    install_eso(apply=True, wait_for_webhook=False, detect=no_operator, probe=no_crds)
    assert len(calls) == 3
    assert not any(c[0] == "kubectl" for c in calls)


def test_webhook_failure_is_reframed(monkeypatch):
    def fake_run(argv):
        if argv[0] == "kubectl":
            raise HelmError("timed out waiting for the condition")
        return _ok(argv)

    patch_run(monkeypatch, fake_run)
    with pytest.raises(HelmError, match="webhook did not become ready"):
        install_eso(apply=True, detect=no_operator, probe=no_crds)


def test_crds_without_an_operator_is_refused(monkeypatch):
    # Installing over somebody else's orphaned CRDs would adopt them.
    calls = []
    patch_run(monkeypatch, lambda argv: calls.append(argv) or _ok(argv))
    with pytest.raises(K8sError, match="resolve by hand"):
        install_eso(apply=True, detect=no_operator, probe=crds_at_v1)
    assert calls == []


# Reusing an operator somebody else installed (D18a) --------------------------


def test_an_existing_operator_is_reused_and_no_helm_command_runs(monkeypatch):
    calls = []
    patch_run(monkeypatch, lambda argv: calls.append(argv) or _ok(argv))
    result = install_eso(
        apply=True, wait_for_webhook=False, detect=existing_operator, probe=crds_at_v1
    )
    assert calls == []
    assert result.reused is True
    assert result.commands == ()
    assert result.summary == "reusing external-secrets v0.19.2 in namespace ns-a"
    assert result.existing == existing_operator()


def test_reuse_carries_the_crd_api_version(monkeypatch):
    patch_run(monkeypatch, _ok)
    result = install_eso(detect=existing_operator, probe=crds_at_v1)
    assert result.api_version == "v1"


def test_the_install_path_carries_no_api_version(monkeypatch):
    patch_run(monkeypatch, _ok)
    result = install_eso(detect=no_operator, probe=no_crds)
    assert result.api_version is None


def test_reuse_waits_for_the_webhook_in_the_operators_own_namespace(monkeypatch):
    calls = []
    patch_run(monkeypatch, lambda argv: calls.append(argv) or _ok(argv))
    result = install_eso(apply=True, detect=existing_operator, probe=crds_at_v1)
    assert len(calls) == 1
    assert calls[0][0] == "kubectl"
    assert calls[0][calls[0].index("--namespace") + 1] == "ns-a"
    assert len(result.commands) == 1


def test_a_reused_operator_with_an_unready_webhook_is_reported_as_such(monkeypatch):
    patch_run(monkeypatch, _raise_timeout)
    with pytest.raises(HelmError, match="being reused"):
        install_eso(apply=True, detect=existing_operator, probe=crds_at_v1)


def test_reuse_is_reported_on_a_dry_run_too(monkeypatch):
    calls = []
    patch_run(monkeypatch, lambda argv: calls.append(argv) or _ok(argv))
    result = install_eso(detect=existing_operator, probe=crds_at_v1)
    assert calls == []
    assert result.reused is True


def test_the_cluster_probes_are_the_defaults():
    # The defaults are the real kubectl probes; the tests inject their own.
    import inspect

    from ladon.install.k8s import detect_eso, eso_api_version

    parameters = inspect.signature(install_eso).parameters
    assert parameters["detect"].default is detect_eso
    assert parameters["probe"].default is eso_api_version


class _Result:
    def __init__(self, argv):
        self.args = argv
        self.returncode = 0
        self.stdout = ""
        self.stderr = ""


def _ok(argv):
    return _Result(argv)


def _raise_timeout(argv):
    raise HelmError("timed out waiting for the condition")
