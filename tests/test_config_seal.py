"""Tests for the pluggable auto-unseal (seal) stanza renderer (D2a)."""

import pytest

from ladon.config.seal import (
    AWS_KMS,
    AZURE_KEY_VAULT,
    GCP_CKMS,
    SHAMIR,
    render_seal,
)
from ladon.config.teams import ConfigError


def test_shamir_when_no_config():
    seal = render_seal(None)
    assert seal.provider == SHAMIR
    assert seal.hcl == ""
    assert seal.fields == {}


def test_shamir_explicit():
    seal = render_seal({"provider": "shamir"})
    assert seal.provider == SHAMIR
    assert seal.hcl == ""


def test_gcpckms_stanza():
    seal = render_seal(
        {
            "provider": "gcpckms",
            "project": "proj",
            "region": "example-region1",
            "key_ring": "ring",
            "crypto_key": "key",
        }
    )
    assert seal.provider == GCP_CKMS
    assert 'seal "gcpckms" {' in seal.hcl
    assert 'project = "proj"' in seal.hcl
    assert 'crypto_key = "key"' in seal.hcl
    assert seal.hcl.endswith("}\n")


def test_gcpckms_missing_field_rejected():
    with pytest.raises(ConfigError, match="gcpckms seal requires a non-empty 'crypto_key'"):
        render_seal({"provider": "gcpckms", "project": "p", "region": "r", "key_ring": "ring"})


def test_awskms_stanza():
    seal = render_seal(
        {"provider": "awskms", "region": "example-region2", "kms_key_id": "abcd-1234"}
    )
    assert seal.provider == AWS_KMS
    assert 'seal "awskms" {' in seal.hcl
    assert 'region = "example-region2"' in seal.hcl
    assert 'kms_key_id = "abcd-1234"' in seal.hcl


def test_awskms_missing_field_rejected():
    with pytest.raises(ConfigError, match="awskms seal requires a non-empty 'kms_key_id'"):
        render_seal({"provider": "awskms", "region": "example-region2"})


def test_azure_managed_identity_no_credentials():
    seal = render_seal(
        {
            "provider": "azurekeyvault",
            "tenant_id": "tenant",
            "vault_name": "vault",
            "key_name": "unseal",
        }
    )
    assert seal.provider == AZURE_KEY_VAULT
    assert 'seal "azurekeyvault" {' in seal.hcl
    assert 'tenant_id = "tenant"' in seal.hcl
    assert "client_id" not in seal.hcl


def test_azure_with_client_id_does_not_render_secret():
    seal = render_seal(
        {
            "provider": "azurekeyvault",
            "tenant_id": "tenant",
            "vault_name": "vault",
            "key_name": "unseal",
            "client_id": "app-123",
            # Even if someone puts a secret-looking field here, it must not render.
            "client_secret": "should-not-appear",
        }
    )
    assert 'client_id = "app-123"' in seal.hcl
    assert "should-not-appear" not in seal.hcl
    assert "client_secret" not in seal.hcl


def test_azure_optional_environment_and_resource():
    seal = render_seal(
        {
            "provider": "azurekeyvault",
            "tenant_id": "t",
            "vault_name": "v",
            "key_name": "k",
            "environment": "AZUREUSGOVERNMENT",
            "resource": "vault.usgovcloudapi.net",
        }
    )
    assert 'environment = "AZUREUSGOVERNMENT"' in seal.hcl
    assert 'resource = "vault.usgovcloudapi.net"' in seal.hcl


def test_azure_missing_required_rejected():
    with pytest.raises(ConfigError, match="azurekeyvault seal requires a non-empty 'key_name'"):
        render_seal({"provider": "azurekeyvault", "tenant_id": "t", "vault_name": "v"})


def test_unknown_provider_is_error_not_silent_shamir():
    with pytest.raises(ConfigError, match="unknown seal provider 'gcpkms'"):
        render_seal({"provider": "gcpkms"})  # typo: missing the 'c'


def test_non_mapping_config_rejected():
    with pytest.raises(ConfigError, match="kms config must be a mapping"):
        render_seal("gcpckms")
