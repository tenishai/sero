"""Helm install of the External Secrets Operator (D4a).

ESO is installed per cluster as an agent-driven, direct Helm install, not via a
GitOps reconciler. It reuses the shared Helm machinery in ``ladon.install.helm``
(``ChartRelease`` for argv construction, ``install`` for the repo-add/update/
upgrade flow, ``run`` for execution), so only ESO's specifics live here: its chart
coordinates, the ``installCRDs`` flag, and a post-apply webhook health check.

Three concerns beyond a plain chart install, called out for D4a and D18a:

* Reuse before install. ESO is cluster-scoped and singular, so an operator
  another team installed is the operator Ladon uses. The install starts by
  looking for one, and when it finds one it reports the version and namespace
  and runs no helm command at all (D18a).
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
from dataclasses import dataclass, field

from ladon.install.helm import ChartRelease, HelmError, install, run
from ladon.install.k8s import ESOInstall, detect_eso

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
    commands: tuple[subprocess.CompletedProcess[str], ...] = field(default_factory=tuple)


def install_eso(
    release: ESORelease | None = None,
    *,
    apply: bool = False,
    wait_for_webhook: bool = True,
    detect: Callable[[], ESOInstall | None] = detect_eso,
) -> ESOInstallResult:
    """Reuse an operator already on the cluster, or install the pinned chart.

    Detection runs first, on a dry-run as well as an apply (D18a): two operators
    fight over one set of CRDs, so an existing one is reported and reused and no
    helm command runs. Detection is injectable so tests and callers that have
    already probed the cluster do not shell out again; a probe failure is an
    error, never a silent second install.

    Otherwise the pinned chart is installed, defaulting to a dry-run: nothing is
    applied unless ``apply=True``. On an apply, after the chart is installed the
    webhook deployment is awaited (unless ``wait_for_webhook=False``), so a
    successful return means the operator is actually ready to admit
    ``ExternalSecret`` resources. On a dry-run the webhook wait is skipped, since
    nothing was installed to wait for.
    """
    release = release or ESORelease()
    existing = detect()
    if existing is not None:
        return ESOInstallResult(
            summary=(
                f"reusing external-secrets {existing.version} in namespace {existing.namespace}"
            ),
            reused=True,
            existing=existing,
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


def _wait_for_webhook(release: ESORelease) -> subprocess.CompletedProcess[str]:
    argv = release.webhook_wait_argv()
    try:
        return run(argv)
    except HelmError as exc:
        # run() raises HelmError for any tool on the path; reframe so the failure
        # reads as the webhook check, not a helm command.
        raise HelmError(f"ESO installed but the webhook did not become ready: {exc}") from exc
