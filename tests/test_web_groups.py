"""Tests for namespace membership from the deployment config (D15).

``ConfigGroupSource`` is the first ``GroupSource``: a mapping in the config, not
a directory. It resolves a caller to namespaces through direct membership and
through group membership, and answers whether the caller is an admin.
"""

from sero.web.groups import ConfigGroupSource
from sero.web.settings import parse_access

ACCESS = parse_access(
    {
        "admins": ["Admin@Example.com"],
        "members": {"op@example.com": ["team-a"]},
        "groups": {"platform": ["team-a", "team-b"], "readers": ["team-c"]},
        "group_members": {"platform": ["Lead@Example.com", "op@example.com"]},
    }
)


def test_direct_membership():
    source = ConfigGroupSource(ACCESS)
    assert source.namespaces_for("lead@example.com") == ["team-a", "team-b"]


def test_group_membership_adds_to_direct_membership_without_duplicates():
    source = ConfigGroupSource(ACCESS)
    assert source.namespaces_for("op@example.com") == ["team-a", "team-b"]


def test_unknown_caller_has_no_namespaces():
    source = ConfigGroupSource(ACCESS)
    assert source.namespaces_for("stranger@example.com") == []


def test_a_group_with_no_members_grants_nothing():
    source = ConfigGroupSource(ACCESS)
    assert "team-c" not in source.namespaces_for("op@example.com")


def test_emails_are_matched_case_insensitively():
    source = ConfigGroupSource(ACCESS)
    assert source.namespaces_for("OP@Example.com") == ["team-a", "team-b"]
    assert source.is_admin("admin@example.com")
    assert source.is_admin("ADMIN@example.com")


def test_a_member_is_not_an_admin():
    source = ConfigGroupSource(ACCESS)
    assert not source.is_admin("op@example.com")


def test_an_empty_access_block_grants_nothing():
    source = ConfigGroupSource(parse_access(None))
    assert source.namespaces_for("op@example.com") == []
    assert not source.is_admin("op@example.com")
