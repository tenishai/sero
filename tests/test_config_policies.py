"""Tests for the baseline OpenBao policy rendering (D3b)."""

from ladon.config import (
    baseline_team_policies,
    external_secrets_read,
    external_secrets_root,
    team_readonly,
    team_readwrite,
)


def test_team_readwrite_grants_full_crud_on_data():
    hcl = team_readwrite().hcl
    assert 'path "kv/data/*"' in hcl
    for cap in ("create", "read", "update", "delete", "list"):
        assert f'"{cap}"' in hcl
    # The soft-delete lifecycle is covered.
    assert 'path "kv/destroy/*"' in hcl


def test_team_readwrite_is_scoped_to_mount_only():
    hcl = team_readwrite().hcl
    # No system or cross-namespace grants in the per-team policy.
    assert "sys/" not in hcl
    assert "auth/" not in hcl


def test_team_readonly_has_no_write_capabilities():
    hcl = team_readonly().hcl
    assert '"read"' in hcl and '"list"' in hcl
    for cap in ("create", "update", "delete"):
        assert f'"{cap}"' not in hcl


def test_external_secrets_read_is_read_only_data():
    p = external_secrets_read()
    assert p.name == "external-secrets-read"
    assert 'path "kv/data/*"' in p.hcl
    assert '"create"' not in p.hcl and '"update"' not in p.hcl


def test_external_secrets_root_reads_across_namespaces():
    p = external_secrets_root()
    assert p.name == "external-secrets-root"
    # The "+" matches one namespace segment: read across all team namespaces.
    assert 'path "+/kv/data/*"' in p.hcl
    assert 'path "+/kv/metadata/*"' in p.hcl
    # Read-only: no write capabilities anywhere.
    for cap in ("create", "update", "delete"):
        assert f'"{cap}"' not in p.hcl


def test_custom_mount_is_respected():
    hcl = team_readwrite(mount="secrets").hcl
    assert 'path "secrets/data/*"' in hcl
    assert "kv/data" not in hcl


def test_baseline_team_policies_names():
    names = {p.name for p in baseline_team_policies()}
    assert names == {"team-readwrite", "team-readonly"}
