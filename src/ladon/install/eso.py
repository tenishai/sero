"""Helm install of the External Secrets Operator (D4a).

ESO is installed per cluster as an agent-driven, direct Helm install, not via a
GitOps reconciler, mirroring the OpenBao install layer (``ladon.install.helm``).
The same dry-run-first contract holds: command construction is pure and testable
without a helm binary, and an apply must be requested explicitly.

Two concerns beyond a plain chart install, called out for D4a:

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
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ladon.install.helm import HelmError, run

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
class ESORelease:
    """A pinned External Secrets Operator Helm release and its commands."""

    name: str = "external-secrets"
    namespace: str = DEFAULT_NAMESPACE
    values_files: Sequence[Path] = field(default_factory=tuple)
    chart: str = ESO_CHART
    version: str = ESO_CHART_VERSION
    create_namespace: bool = True
    install_crds: bool = True

    def repo_add_argv(self) -> list[str]:
        return ["helm", "repo", "add", ESO_REPO_NAME, ESO_REPO_URL]

    def repo_update_argv(self) -> list[str]:
        return ["helm", "repo", "update", ESO_REPO_NAME]

    def upgrade_argv(self, *, dry_run: bool = True) -> list[str]:
        """Build the ``helm upgrade --install`` command.

        Defaults to a dry-run. The chart version is always pinned explicitly so
        the command never tracks a floating chart. CRD installation is set
        explicitly so the operator owns its CRDs.
        """
        argv = [
            "helm",
            "upgrade",
            "--install",
            self.name,
            self.chart,
            "--version",
            self.version,
            "--namespace",
            self.namespace,
        ]
        if self.create_namespace:
            argv.append("--create-namespace")
        argv += ["--set", f"installCRDs={'true' if self.install_crds else 'false'}"]
        for values_file in self.values_files:
            argv += ["-f", str(values_file)]
        if dry_run:
            argv.append("--dry-run")
        return argv

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


def install_eso(
    release: ESORelease | None = None,
    *,
    apply: bool = False,
    wait_for_webhook: bool = True,
) -> list[subprocess.CompletedProcess[str]]:
    """Add and refresh the pinned repo, then upgrade-install ESO.

    Defaults to a dry-run: nothing is applied unless ``apply=True``. On an apply,
    after the chart is installed the webhook deployment is awaited (unless
    ``wait_for_webhook=False``), so a successful return means the operator is
    actually ready to admit ``ExternalSecret`` resources. On a dry-run the
    webhook wait is skipped, since nothing was installed to wait for.
    """
    release = release or ESORelease()
    results = [
        run(release.repo_add_argv()),
        run(release.repo_update_argv()),
        run(release.upgrade_argv(dry_run=not apply)),
    ]
    if apply and wait_for_webhook:
        results.append(_wait_for_webhook(release))
    return results


def _wait_for_webhook(release: ESORelease) -> subprocess.CompletedProcess[str]:
    argv = release.webhook_wait_argv()
    try:
        return run(argv)
    except HelmError as exc:
        # run() raises HelmError for any tool on the path; reframe so the failure
        # reads as the webhook check, not a helm command.
        raise HelmError(f"ESO installed but the webhook did not become ready: {exc}") from exc
