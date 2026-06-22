"""Tests for deriving plan inputs from a deployment config."""

import pytest

from ladon.config.derive import kv_mount, team_namespace
from ladon.config.loader import DeploymentConfig
from ladon.config.policies import DEFAULT_KV_MOUNT
from ladon.config.teams import ConfigError, Team


def make_config(**settings):
    teams = [
        Team(name="team-a", namespace="ns-a", environments=("dev",)),
        Team(name="team-b", namespace="ns-b", environments=("prod",)),
    ]
    return DeploymentConfig(teams=teams, settings=settings)


def test_kv_mount_defaults_when_unset():
    assert kv_mount(make_config()) == DEFAULT_KV_MOUNT


def test_kv_mount_reads_setting():
    assert kv_mount(make_config(kv_mount="team-kv")) == "team-kv"


def test_kv_mount_rejects_non_string():
    with pytest.raises(ConfigError, match="kv_mount must be a non-empty string"):
        kv_mount(make_config(kv_mount=42))


def test_kv_mount_rejects_empty():
    with pytest.raises(ConfigError, match="kv_mount must be a non-empty string"):
        kv_mount(make_config(kv_mount=""))


def test_team_namespace_resolves_by_name():
    assert team_namespace(make_config(), "team-a") == "ns-a"
    assert team_namespace(make_config(), "team-b") == "ns-b"


def test_team_namespace_unknown_team_lists_known():
    with pytest.raises(ConfigError, match="no team named 'nope'.*team-a, team-b"):
        team_namespace(make_config(), "nope")
