"""Derive apply-plan inputs from a loaded deployment configuration.

The loader (``sero.config.loader``) parses a config file into a
``DeploymentConfig`` (typed teams plus raw ``settings`` / ``sync_targets``
mappings). This module reads the parts a plan needs out of that config: the KV
mount path the deployment uses, and the OpenBao namespace for a named team.

It deliberately does not derive auth-method inputs (workload JWT discovery URL,
OIDC client id and secret reference): the baseline config does not model those,
and adding that schema is a separate decision. Those stay explicit CLI inputs;
this module supplies only what the config already carries.

Pure: it reads an in-memory ``DeploymentConfig`` and returns values, touching no
files and no OpenBao.
"""

from __future__ import annotations

import re

from sero.config.loader import DeploymentConfig
from sero.config.policies import DEFAULT_KV_MOUNT
from sero.config.teams import ConfigError

# A mount is one path segment. The apply layer takes the last segment of
# "sys/mounts/<mount>" as the mount name, so a value with a separator in it
# would enable a mount other than the one the config names.
_MOUNT_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


def kv_mount(config: DeploymentConfig) -> str:
    """The deployment's KV mount path, or the baseline default if unset.

    Read from ``deployment.kv_mount`` when present. The value must be a non-empty
    string and a single path segment; anything else is a config error rather than
    a silent fallback.
    """
    value = config.settings.get("kv_mount")
    if value is None:
        return DEFAULT_KV_MOUNT
    if not isinstance(value, str) or not value:
        raise ConfigError("deployment.kv_mount must be a non-empty string")
    if not _MOUNT_RE.match(value):
        raise ConfigError(
            f"deployment.kv_mount {value!r} must be a single path segment "
            "(letters, digits, and _ . -)"
        )
    return value


def team_namespace(config: DeploymentConfig, team_name: str) -> str:
    """The OpenBao namespace for the named team.

    Resolves a team by ``name`` to its configured ``namespace``, so a caller can
    name a team and have the namespace come from the one place it is defined,
    rather than passing it separately and risking a mismatch.
    """
    for team in config.teams:
        if team.name == team_name:
            return team.namespace
    known = ", ".join(sorted(t.name for t in config.teams)) or "(none)"
    raise ConfigError(f"no team named {team_name!r} in config; known teams: {known}")
