"""Load a deployment configuration file into the customization model.

A deployment is described by configuration, not by edits to Sero
(docs/baseline.md). This module reads that config (YAML) and parses it into a
``DeploymentConfig``: the validated teams, plus the deployment settings and sync
targets kept as-is for the layers that consume them.

The config holds names, paths, and references only, never secret values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from sero.config.teams import ConfigError, Team, parse_teams


@dataclass(frozen=True)
class DeploymentConfig:
    """One deployment's configuration."""

    teams: list[Team] = field(default_factory=list)
    # Deployment settings (cloud, region, OIDC, KMS) and sync targets are kept
    # as plain mappings here; the layers that consume them define their own
    # schemas as they land.
    settings: dict = field(default_factory=dict)
    sync_targets: dict = field(default_factory=dict)
    # Who the caller is (the trusted proxy header) and what they may reach
    # (namespaces by email and by group, plus the admins). The backend's
    # settings module owns their schema; see sero.web.settings.
    identity: dict = field(default_factory=dict)
    access: dict = field(default_factory=dict)
    # The machines permitted to write on a person's behalf (D16). A list, not a
    # mapping: order does not matter, and each entry carries its own name.
    service_callers: list = field(default_factory=list)


# The top-level sections this loader reads. A key outside this set is a setting
# nobody has (a misspelling loads silently and does nothing), so it fails here.
KNOWN_TOP_LEVEL_KEYS = frozenset(
    {"deployment", "sync_targets", "identity", "access", "service_callers", "teams"}
)


def parse_deployment_config(data: object) -> DeploymentConfig:
    """Parse an already-loaded config mapping into a DeploymentConfig."""
    if data is None:
        return DeploymentConfig()
    if not isinstance(data, dict):
        raise ConfigError(f"top-level config must be a mapping, got {type(data).__name__}")

    unknown = sorted(set(data) - KNOWN_TOP_LEVEL_KEYS)
    if unknown:
        raise ConfigError(
            f"unknown top-level key(s) {', '.join(repr(k) for k in unknown)}; "
            f"known: {', '.join(sorted(KNOWN_TOP_LEVEL_KEYS))}"
        )

    settings = data.get("deployment") or {}
    if not isinstance(settings, dict):
        raise ConfigError("'deployment' must be a mapping")

    sync_targets = data.get("sync_targets") or {}
    if not isinstance(sync_targets, dict):
        raise ConfigError("'sync_targets' must be a mapping")

    identity = data.get("identity") or {}
    if not isinstance(identity, dict):
        raise ConfigError("'identity' must be a mapping")

    access = data.get("access") or {}
    if not isinstance(access, dict):
        raise ConfigError("'access' must be a mapping")

    service_callers = data.get("service_callers") or []
    if not isinstance(service_callers, list):
        raise ConfigError("'service_callers' must be a list")

    return DeploymentConfig(
        teams=parse_teams(data.get("teams")),
        settings=settings,
        sync_targets=sync_targets,
        identity=identity,
        access=access,
        service_callers=service_callers,
    )


def load_deployment_config_text(text: str) -> DeploymentConfig:
    """Parse a deployment config from a YAML string."""
    return parse_deployment_config(yaml.safe_load(text))


def load_deployment_config(path: str | Path) -> DeploymentConfig:
    """Load and parse a deployment config from a YAML file."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    return load_deployment_config_text(path.read_text(encoding="utf-8"))
