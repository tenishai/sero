"""Helm command construction and execution for the OpenBao install layer (D3a).

The install layer is a direct Helm install, not the Terraform Helm provider, so
OpenBao can be installed, upgraded, and rolled back independently of the cloud
substrate. See docs/design.md (D3a) and deploy/openbao/README.md.

Command construction (the argv builders on ``HelmRelease``) is pure and easy to
test without a helm binary. Execution is a thin wrapper that defaults to a
dry-run; an apply must be requested explicitly, per the secrets-hygiene rule that
writes are dry-run by default.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

# Upstream chart coordinates. The chart is not vendored; we install the pinned
# version from the upstream repo. Keep the version in sync with docs/versions.md;
# bumps are their own reviewed PR.
OPENBAO_REPO_NAME = "openbao"
OPENBAO_REPO_URL = "https://openbao.github.io/openbao-helm"
OPENBAO_CHART = "openbao/openbao"
OPENBAO_CHART_VERSION = "0.28.4"


class HelmError(RuntimeError):
    """A helm invocation failed or helm is unavailable."""


@dataclass(frozen=True)
class HelmRelease:
    """A pinned OpenBao Helm release and the commands that manage it."""

    name: str
    namespace: str
    values_files: Sequence[Path] = field(default_factory=tuple)
    chart: str = OPENBAO_CHART
    version: str = OPENBAO_CHART_VERSION
    create_namespace: bool = True

    def repo_add_argv(self) -> list[str]:
        return ["helm", "repo", "add", OPENBAO_REPO_NAME, OPENBAO_REPO_URL]

    def repo_update_argv(self) -> list[str]:
        return ["helm", "repo", "update", OPENBAO_REPO_NAME]

    def upgrade_argv(self, *, dry_run: bool = True) -> list[str]:
        """Build the ``helm upgrade --install`` command.

        Defaults to a dry-run. The chart version is always pinned explicitly so
        the command never tracks a floating chart.
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
        for values_file in self.values_files:
            argv += ["-f", str(values_file)]
        if dry_run:
            argv.append("--dry-run")
        return argv


def run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a helm command, raising HelmError on failure or if helm is missing.

    Output is captured and returned; callers decide what to surface. Never log
    rendered manifests or values blindly: they can contain references that
    should not reach logs.
    """
    if shutil.which(argv[0]) is None:
        raise HelmError(
            f"{argv[0]!r} not found on PATH; install the pinned helm (see docs/versions.md)"
        )
    result = subprocess.run(argv, capture_output=True, text=True)  # noqa: S603
    if result.returncode != 0:
        raise HelmError(f"command failed ({result.returncode}): {' '.join(argv)}\n{result.stderr}")
    return result


def install(release: HelmRelease, *, apply: bool = False) -> list[subprocess.CompletedProcess[str]]:
    """Add and refresh the pinned repo, then upgrade-install the release.

    Defaults to a dry-run: nothing is applied unless ``apply=True`` is passed
    explicitly. This mirrors the dry-run-first apply model in docs/design.md.
    """
    results = [
        run(release.repo_add_argv()),
        run(release.repo_update_argv()),
        run(release.upgrade_argv(dry_run=not apply)),
    ]
    return results
