"""Tests for the deployment config loader."""

from pathlib import Path

import pytest

from ladon.config import (
    ConfigError,
    DeploymentConfig,
    load_deployment_config,
    load_deployment_config_text,
    parse_deployment_config,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example.yaml"


def test_loads_committed_example():
    cfg = load_deployment_config(EXAMPLE)
    assert {t.namespace for t in cfg.teams} == {"team-a", "team-b"}
    assert cfg.settings["environment"] == "dev"
    assert cfg.sync_targets["kubernetes"]["enabled"] is True
    # The example must never carry secret material.
    assert "key_ref" in cfg.settings["kms"]
    assert cfg.settings["kms"]["key_ref"].startswith("<")


def test_empty_config_is_valid():
    cfg = load_deployment_config_text("")
    assert cfg == DeploymentConfig()
    assert cfg.teams == []


def test_text_parses_teams_and_settings():
    cfg = load_deployment_config_text(
        """
        deployment:
          environment: prod
        teams:
          - name: payments
            environments: [prod]
        """
    )
    assert cfg.settings["environment"] == "prod"
    assert cfg.teams[0].namespace == "payments"


def test_non_mapping_top_level_rejected():
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_deployment_config_text("- just\n- a\n- list\n")


def test_bad_deployment_section_rejected():
    with pytest.raises(ConfigError, match="'deployment' must be a mapping"):
        load_deployment_config_text("deployment: not-a-mapping\n")


def test_team_validation_propagates():
    with pytest.raises(ConfigError, match="environments"):
        load_deployment_config_text("teams:\n  - name: x\n")


def test_missing_file_rejected(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_deployment_config(tmp_path / "nope.yaml")


def test_parse_deployment_config_none_is_empty():
    assert parse_deployment_config(None) == DeploymentConfig()
