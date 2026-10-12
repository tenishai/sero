"""Helm install of the External Secrets Operator (D4a).

ESO is installed per cluster as an agent-driven, direct Helm install, not via a
GitOps reconciler. It reuses the shared Helm machinery in ``sero.install.helm``
(``ChartRelease`` for argv construction, ``install`` for the repo-add/update/
upgrade flow, ``run`` for execution), so only ESO's specifics live here: its chart
coordinates, the ``installCRDs`` flag, and a post-apply webhook health check.

Three concerns beyond a plain chart install, called out for D4a and D18a:

* Reuse before install. ESO is cluster-scoped and singular, so an operator
  another team installed is the operator Sero uses. The install starts by
  looking for one, and when it finds one it reports the version and namespace,
  carries the CRD storage version it probed, and runs no helm command at all
  (D18a). Custom resource definitions with no operator behind them are a
  refusal rather than an install, since the chart would adopt them.
* CRD ownership. The chart installs its CustomResourceDefinitions when
  ``installCRDs`` is set; we set it explicitly so the operator and its CRDs are
  one managed unit rather than relying on a separate CRD step.
* Webhook health. ESO ships a validating webhook that must be reachable before
  ``ExternalSecret`` resources apply cleanly. After an apply, the install waits
  for the webhook deployment to become available, so a green install means a
  usable operator.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from sero.install.helm import ChartRelease, HelmError, install, run
from sero.install.k8s import (
    ESOInstall,
    ESONotInstalledError,
    K8sError,
    detect_eso,
    eso_api_version,
)

# Upstream chart coordinates. Not vendored; install the pinned version from the
# upstream repo. Keep in sync with docs/versions.md; bumps are their own PR.
ESO_REPO_NAME = "external-secrets"
ESO_REPO_URL = "https://charts.external-secrets.io"
ESO_CHART = "external-secrets/external-secrets"
ESO_CHART_VERSION = "2.6.0"

# The webhook deployment the chart creates; its readiness gates usable installs.
ESO_WEBHOOK_DEPLOYMENT = "external-secrets-webhook"
DEFAULT_NAMESPACE = "external-secrets"
DEFAULT_WEBHOOK_TIMEOUT = "120s"


@dataclass(frozen=True)
class ESORelease(ChartRelease):
    """A pinned External Secrets Operator Helm release."""

    name: str = "external-secrets"
    namespace: str = DEFAULT_NAMESPACE
    chart: str = ESO_CHART
    version: str = ESO_CHART_VERSION
    repo_name: str = ESO_REPO_NAME
    repo_url: str = ESO_REPO_URL
    install_crds: bool = True

    def extra_set_args(self) -> list[str]:
        # Set CRD installation explicitly so the operator owns its CRDs.
        return ["--set", f"installCRDs={'true' if self.install_crds else 'false'}"]

    def webhook_wait_argv(self, *, timeout: str = DEFAULT_WEBHOOK_TIMEOUT) -> list[str]:
        """Build the ``kubectl wait`` command for the webhook deployment.

        A usable ESO needs its validating webhook available; this waits for that
        deployment to report Available before the install is considered done.
        """
        return [
            "kubectl",
            "wait",
            "--namespace",
            self.namespace,
            "--for=condition=Available",
            f"deployment/{ESO_WEBHOOK_DEPLOYMENT}",
            f"--timeout={timeout}",
        ]


@dataclass(frozen=True)
class ESOInstallResult:
    """What the install did: reused an operator, or installed the pinned chart."""

    summary: str
    reused: bool
    existing: ESOInstall | None = None
    api_version: str | None = None
    commands: tuple[subprocess.CompletedProcess[str], ...] = field(default_factory=tuple)


def install_eso(
    release: ESORelease | None = None,
    *,
    apply: bool = False,
    wait_for_webhook: bool = True,
    detect: Callable[[], ESOInstall | None] = detect_eso,
    probe: Callable[[], str] = eso_api_version,
) -> ESOInstallResult:
    """Reuse an operator already on the cluster, or install the pinned chart.

    Detection runs first, on a dry-run as well as an apply (D18a): two operators
    fight over one set of CRDs, so an existing one is reported and reused and no
    helm command runs. The reused operator's CRD storage version is probed and
    carried on the result, because that is the version resources must be
    rendered at, and its webhook is awaited in its own namespace rather than the
    one this release would have installed into.

    With no operator, the CRDs must be absent too. CRDs without an operator is a
    refusal, not an install: the chart would adopt custom resource definitions
    somebody else owns, which is the mismatch that broke the source deployment.

    Otherwise the pinned chart is installed, defaulting to a dry-run: nothing is
    applied unless ``apply=True``. On an apply, after the chart is installed the
    webhook deployment is awaited (unless ``wait_for_webhook=False``), so a
    successful return means the operator is actually ready to admit
    ``ExternalSecret`` resources. On a dry-run the webhook wait is skipped, since
    nothing was installed to wait for.

    Both cluster probes are injectable so tests and callers that have already
    looked do not shell out again; a probe failure is an error, never a silent
    second install.
    """
    release = release or ESORelease()
    existing = detect()
    if existing is not None:
        # Probe before the wait: it is a cheap read, and an operator whose CRDs
        # are missing is not worth waiting two minutes for.
        api_version = probe()
        commands = []
        if apply and wait_for_webhook:
            commands.append(
                _wait_for_webhook(replace(release, namespace=existing.namespace), reused=True)
            )
        return ESOInstallResult(
            summary=(
                f"reusing external-secrets {existing.version} in namespace {existing.namespace}"
            ),
            reused=True,
            existing=existing,
            api_version=api_version,
            commands=tuple(commands),
        )

    try:
        probe()
    except ESONotInstalledError:
        pass
    else:
        raise K8sError(
            "external-secrets CRDs exist but no operator deployment was found; resolve by hand"
        )

    results = install(release, apply=apply)
    if apply and wait_for_webhook:
        results.append(_wait_for_webhook(release))
    verb = "installed" if apply else "dry run: would install"
    return ESOInstallResult(
        summary=(
            f"{verb} external-secrets chart {release.version} in namespace {release.namespace}"
        ),
        reused=False,
        commands=tuple(results),
    )


def _wait_for_webhook(
    release: ESORelease, *, reused: bool = False
) -> subprocess.CompletedProcess[str]:
    argv = release.webhook_wait_argv()
    try:
        return run(argv)
    except HelmError as exc:
        # run() raises HelmError for any tool on the path; reframe so the failure
        # reads as the webhook check, not a helm command, and name which operator
        # it was: a reused one is somebody else's to fix.
        subject = (
            f"the external-secrets operator being reused in namespace {release.namespace}"
            if reused
            else "ESO installed but the operator"
        )
        raise HelmError(f"{subject}: webhook did not become ready: {exc}") from exc
