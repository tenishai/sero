"""Tests for the Helm values overlay that carries the seal stanza (D2a).

The renderer is pure: it takes the raft HCL a deployment already runs, the
rendered seal stanza, and any service-account annotations, and returns the
overlay YAML. Nothing here reads a file or contacts a cluster.
"""

import pytest
import yaml

from ladon.config.seal import render_seal
from ladon.config.seal_values import render_seal_values
from ladon.config.teams import ConfigError

BASE_HCL = """ui = true

listener "tcp" {
  tls_disable = 1
  address     = "[::]:8200"
}

storage "raft" {
  path = "/openbao/data"
}

service_registration "kubernetes" {}
"""

GCP_KMS = {
    "provider": "gcpckms",
    "project": "example-project",
    "region": "example-location1",
    "key_ring": "example-ring",
    "crypto_key": "example-key",
}


def overlay(base_hcl=BASE_HCL, kms=None, annotations=None):
    """The parsed overlay, so assertions read the document rather than the text."""
    text = render_seal_values(base_hcl, render_seal(kms or GCP_KMS), annotations or {})
    return yaml.safe_load(text), text


def config_hcl(document):
    return document["server"]["ha"]["raft"]["config"]


def test_overlay_sets_only_the_raft_config():
    document, _ = overlay()
    assert list(document) == ["server"]
    assert list(document["server"]) == ["ha"]


def test_seal_stanza_lands_before_service_registration():
    document, _ = overlay()
    hcl = config_hcl(document)
    assert 'seal "gcpckms" {' in hcl
    assert 'crypto_key = "example-key"' in hcl
    assert hcl.index('seal "gcpckms"') < hcl.index("service_registration")


def test_base_hcl_is_carried_through_unchanged():
    document, _ = overlay()
    hcl = config_hcl(document)
    for line in ('listener "tcp" {', 'path = "/openbao/data"', "ui = true"):
        assert line in hcl
    # The listener and storage blocks keep their order relative to each other.
    assert hcl.index('listener "tcp"') < hcl.index('storage "raft"')


def test_config_renders_as_a_block_scalar():
    # Helm values carry the server config as a literal block. A quoted scalar
    # would be valid YAML and an unreadable diff.
    _, text = overlay()
    assert "config: |" in text
    assert "\\n" not in text


def test_seal_stanza_is_appended_when_there_is_no_service_registration():
    base = 'ui = true\n\nstorage "raft" {\n  path = "/openbao/data"\n}\n'
    document, _ = overlay(base_hcl=base)
    hcl = config_hcl(document)
    assert hcl.index('storage "raft"') < hcl.index('seal "gcpckms"')
    assert hcl.endswith("}\n")


def test_annotations_are_rendered_on_the_server_service_account():
    document, _ = overlay(
        annotations={"iam.gke.io/gcp-service-account": "unseal@example.example.com"}
    )
    annotations = document["server"]["serviceAccount"]["annotations"]
    assert annotations == {"iam.gke.io/gcp-service-account": "unseal@example.example.com"}


def test_no_service_account_key_without_annotations():
    document, _ = overlay()
    assert "serviceAccount" not in document["server"]


def test_shamir_is_refused_because_there_is_no_stanza_to_render():
    with pytest.raises(ConfigError, match="shamir"):
        render_seal_values(BASE_HCL, render_seal({"provider": "shamir"}), {})


def test_empty_base_config_is_refused():
    with pytest.raises(ConfigError, match="base server config is empty"):
        render_seal_values("   \n", render_seal(GCP_KMS), {})


def test_trailing_whitespace_does_not_break_the_block_scalar():
    base = 'ui = true   \n\nservice_registration "kubernetes" {}\n'
    _, text = overlay(base_hcl=base)
    assert "config: |" in text
    assert yaml.safe_load(text)["server"]["ha"]["raft"]["config"].startswith("ui = true\n")


def test_azure_client_secret_never_reaches_the_overlay():
    document, _ = overlay(
        kms={
            "provider": "azurekeyvault",
            "tenant_id": "example-tenant",
            "vault_name": "example-vault",
            "key_name": "example-unseal",
            "client_id": "example-app",
            "client_secret": "should-not-appear",
        }
    )
    hcl = config_hcl(document)
    assert 'seal "azurekeyvault" {' in hcl
    assert "should-not-appear" not in hcl
    assert "client_secret" not in hcl
