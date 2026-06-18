"""The teams part of the customization model (docs/baseline.md).

A deployment lists its teams in configuration; Ladon creates a namespace per
team with the baseline policy set. This module parses and validates that team
config into ``Team`` objects and maps each to its rendered baseline policies.

Parsing operates on plain dicts (already loaded from YAML or JSON), so it stays
dependency-free and pure. Loading the config file is a separate concern.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ladon.config.policies import (
    DEFAULT_KV_MOUNT,
    Policy,
    team_readonly,
    team_readwrite,
)

# Namespace and team names: lowercase DNS-label style. Generic and portable; no
# deployment-specific convention is assumed.
_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

_ACCESS_LEVELS = frozenset({"readwrite", "readonly"})


class ConfigError(ValueError):
    """Raised when team configuration is missing or invalid."""


@dataclass(frozen=True)
class Team:
    """One team: a namespace, the environments it exists in, and its access."""

    name: str
    namespace: str
    environments: tuple[str, ...]
    groups: tuple[str, ...] = ()
    access: str = "readwrite"

    @classmethod
    def from_dict(cls, data: dict) -> Team:
        if not isinstance(data, dict):
            raise ConfigError(f"team entry must be a mapping, got {type(data).__name__}")

        name = data.get("name")
        if not name or not isinstance(name, str):
            raise ConfigError("team requires a non-empty 'name'")

        # Namespace defaults to the name when not given.
        namespace = data.get("namespace") or name
        if not _NAME_RE.match(namespace):
            raise ConfigError(
                f"team {name!r}: namespace {namespace!r} must be a lowercase "
                "DNS-label (a-z, 0-9, hyphen)"
            )

        environments = data.get("environments") or []
        if not isinstance(environments, list) or not environments:
            raise ConfigError(f"team {name!r}: 'environments' must be a non-empty list")
        if not all(isinstance(e, str) and e for e in environments):
            raise ConfigError(f"team {name!r}: every environment must be a non-empty string")

        groups = data.get("groups") or []
        if not isinstance(groups, list) or not all(isinstance(g, str) and g for g in groups):
            raise ConfigError(f"team {name!r}: 'groups' must be a list of strings")

        access = data.get("access", "readwrite")
        if access not in _ACCESS_LEVELS:
            raise ConfigError(
                f"team {name!r}: access {access!r} must be one of {sorted(_ACCESS_LEVELS)}"
            )

        return cls(
            name=name,
            namespace=namespace,
            environments=tuple(environments),
            groups=tuple(groups),
            access=access,
        )

    def policies(self, mount: str = DEFAULT_KV_MOUNT) -> list[Policy]:
        """The baseline policies this team gets, rendered for its KV mount."""
        if self.access == "readonly":
            return [team_readonly(mount)]
        return [team_readwrite(mount)]


def parse_teams(items: list) -> list[Team]:
    """Parse and validate a list of team entries.

    Rejects duplicate namespaces, which would collide in OpenBao. An empty list
    is valid: a deployment may have no teams yet.
    """
    if items is None:
        return []
    if not isinstance(items, list):
        raise ConfigError(f"'teams' must be a list, got {type(items).__name__}")

    teams = [Team.from_dict(item) for item in items]

    seen: dict[str, str] = {}
    for team in teams:
        if team.namespace in seen:
            raise ConfigError(
                f"duplicate namespace {team.namespace!r} (teams {seen[team.namespace]!r} "
                f"and {team.name!r})"
            )
        seen[team.namespace] = team.name

    return teams
