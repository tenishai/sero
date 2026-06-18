"""Tests for the teams config model (D3b customization model)."""

import pytest

from ladon.config import ConfigError, Team, parse_teams


def test_parse_minimal_team():
    [team] = parse_teams([{"name": "payments", "environments": ["dev"]}])
    assert team.name == "payments"
    assert team.namespace == "payments"  # defaults to name
    assert team.environments == ("dev",)
    assert team.access == "readwrite"


def test_namespace_can_differ_from_name():
    [team] = parse_teams(
        [{"name": "Payments Team", "namespace": "payments", "environments": ["dev"]}]
    )
    assert team.namespace == "payments"


def test_empty_list_is_valid():
    assert parse_teams([]) == []
    assert parse_teams(None) == []


def test_missing_name_rejected():
    with pytest.raises(ConfigError, match="name"):
        parse_teams([{"environments": ["dev"]}])


def test_missing_environments_rejected():
    with pytest.raises(ConfigError, match="environments"):
        parse_teams([{"name": "payments"}])


def test_invalid_namespace_rejected():
    with pytest.raises(ConfigError, match="DNS-label"):
        parse_teams([{"name": "x", "namespace": "Bad_NS", "environments": ["dev"]}])


def test_invalid_access_rejected():
    with pytest.raises(ConfigError, match="access"):
        parse_teams([{"name": "x", "environments": ["dev"], "access": "admin"}])


def test_duplicate_namespace_rejected():
    with pytest.raises(ConfigError, match="duplicate namespace"):
        parse_teams(
            [
                {"name": "a", "namespace": "shared", "environments": ["dev"]},
                {"name": "b", "namespace": "shared", "environments": ["dev"]},
            ]
        )


def test_readonly_team_gets_readonly_policy():
    [team] = parse_teams([{"name": "viewer", "environments": ["dev"], "access": "readonly"}])
    names = [p.name for p in team.policies()]
    assert names == ["team-readonly"]


def test_readwrite_team_gets_readwrite_policy():
    [team] = parse_teams([{"name": "payments", "environments": ["dev", "prod"]}])
    names = [p.name for p in team.policies()]
    assert names == ["team-readwrite"]


def test_groups_parsed():
    [team] = parse_teams(
        [{"name": "payments", "environments": ["dev"], "groups": ["payments-admins"]}]
    )
    assert team.groups == ("payments-admins",)


def test_from_dict_rejects_non_mapping():
    with pytest.raises(ConfigError, match="mapping"):
        Team.from_dict(["not", "a", "dict"])
