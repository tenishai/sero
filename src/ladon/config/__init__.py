"""OpenBao configuration layer (D3b): render baseline config from parameters."""

from ladon.config.auth import (
    DEFAULT_TOKEN_TTL,
    AuthMethod,
    AuthRole,
    jwt_auth,
    jwt_role,
    oidc_auth,
    oidc_role,
)
from ladon.config.loader import (
    DeploymentConfig,
    load_deployment_config,
    load_deployment_config_text,
    parse_deployment_config,
)
from ladon.config.policies import (
    DEFAULT_KV_MOUNT,
    Policy,
    baseline_team_policies,
    external_secrets_read,
    external_secrets_root,
    team_readonly,
    team_readwrite,
)
from ladon.config.teams import ConfigError, Team, parse_teams

__all__ = [
    "DEFAULT_KV_MOUNT",
    "DEFAULT_TOKEN_TTL",
    "AuthMethod",
    "AuthRole",
    "ConfigError",
    "DeploymentConfig",
    "Policy",
    "Team",
    "baseline_team_policies",
    "external_secrets_read",
    "external_secrets_root",
    "jwt_auth",
    "jwt_role",
    "load_deployment_config",
    "load_deployment_config_text",
    "oidc_auth",
    "oidc_role",
    "parse_deployment_config",
    "parse_teams",
    "team_readonly",
    "team_readwrite",
]
