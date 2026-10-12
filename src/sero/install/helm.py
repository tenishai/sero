"""Helm command construction and execution for the install layers (D3a/D4a).

Sero installs OpenBao and the External Secrets Operator through direct Helm
installs (not the Terraform Helm provider), so each can be installed, upgraded,
and rolled back independently of the cloud substrate. See docs/design.md
(D3a/D4a).

``ChartRelease`` is the shared base: it holds a chart's repo coordinates and
builds the ``helm repo add`` / ``repo update`` / ``upgrade --install`` commands.
Both the OpenBao release (``HelmRelease``, below) and the ESO release
(``sero.install.eso``) subclass it, so the argv construction and the install
flow live in one place. Command construction is pure and testable without a helm
binary. Execution is a thin wrapper that defaults to a dry-run; an apply must be
requested explicitly, per the secrets-hygiene rule that writes are dry-run by
default.
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
class ChartRelease:
    """A pinned Helm release and the commands that manage it.

    Subclasses set the repo coordinates (``repo_name``, ``repo_url``) and the
    chart defaults. The chart version is always pinned explicitly so a command
    never tracks a floating chart. Subclasses may add install-time ``--set`` flags
    by overriding ``extra_set_args``.
    """

    name: str
    namespace: str
    values_files: Sequence[Path] = field(default_factory=tuple)
    chart: str = ""
    version: str = ""
    create_namespace: bool = True

    # Repo coordinates. Class-level so subclasses set them once; instances do not
    # need to carry them.
    repo_name: str = ""
    repo_url: str = ""

    def extra_set_args(self) -> list[str]:
        """Extra ``--set key=value`` args for this chart. Overridden per chart."""
        return []

    def repo_add_argv(self) -> list[str]:
        return ["helm", "repo", "add", self.repo_name, self.repo_url]

    def repo_update_argv(self) -> list[str]:
        return ["helm", "repo", "update", self.repo_name]

    def upgrade_argv(self, *, dry_run: bool = True) -> list[str]:
        """Build the ``helm upgrade --install`` command.

        Defaults to a dry-run. The chart version is always pinned explicitly.
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
        argv += self.extra_set_args()
        for values_file in self.values_files:
            argv += ["-f", str(values_file)]
        if dry_run:
            argv.append("--dry-run")
        return argv


@dataclass(frozen=True)
class HelmRelease(ChartRelease):
    """A pinned OpenBao Helm release."""

    chart: str = OPENBAO_CHART
    version: str = OPENBAO_CHART_VERSION
    repo_name: str = OPENBAO_REPO_NAME
    repo_url: str = OPENBAO_REPO_URL


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


def install(
    release: ChartRelease, *, apply: bool = False
) -> list[subprocess.CompletedProcess[str]]:
    """Add and refresh the pinned repo, then upgrade-install the release.

    Defaults to a dry-run: nothing is applied unless ``apply=True`` is passed
    explicitly. This mirrors the dry-run-first apply model in docs/design.md.
    """
    return [
        run(release.repo_add_argv()),
        run(release.repo_update_argv()),
        run(release.upgrade_argv(dry_run=not apply)),
    ]
