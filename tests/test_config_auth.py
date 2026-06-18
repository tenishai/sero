"""Tests for the baseline auth-method rendering."""

import pytest

from ladon.config import (
    ConfigError,
    kubernetes_auth,
    kubernetes_role,
    oidc_auth,
    oidc_role,
)


def test_kubernetes_auth_minimal():
    method = kubernetes_auth(host="https://kube.example.com")
    assert method.type == "kubernetes"
    assert method.path == "kubernetes"
    assert method.config == {"kubernetes_host": "https://kube.example.com"}
    assert method.roles == ()


def test_kubernetes_auth_ca_cert_is_a_reference_not_a_value():
    method = kubernetes_auth(
        host="https://kube.example.com",
        ca_cert_ref="cluster-ca",
    )
    # The CA is carried as a reference key, never an inline cert.
    assert method.config["kubernetes_ca_cert_ref"] == "cluster-ca"
    assert "kubernetes_ca_cert" not in method.config


def test_kubernetes_auth_requires_host():
    with pytest.raises(ConfigError, match="host"):
        kubernetes_auth(host="")


def test_kubernetes_role_binds_all_three():
    role = kubernetes_role(
        "team-a",
        service_accounts=["sa-a"],
        namespaces=["ns-a"],
        policies=["team-readwrite"],
    )
    assert role.data["bound_service_account_names"] == ["sa-a"]
    assert role.data["bound_service_account_namespaces"] == ["ns-a"]
    assert role.data["token_policies"] == ["team-readwrite"]
    assert role.data["token_ttl"] == "1h"


@pytest.mark.parametrize(
    ("sa", "ns", "pol"),
    [
        ([], ["ns"], ["p"]),
        (["sa"], [], ["p"]),
        (["sa"], ["ns"], []),
    ],
)
def test_kubernetes_role_rejects_empty_bindings(sa, ns, pol):
    with pytest.raises(ConfigError):
        kubernetes_role("r", service_accounts=sa, namespaces=ns, policies=pol)


def test_kubernetes_auth_carries_roles():
    role = kubernetes_role("team-a", service_accounts=["sa"], namespaces=["ns"], policies=["p"])
    method = kubernetes_auth(host="https://k", roles=[role])
    assert method.roles == (role,)


def test_oidc_auth_client_secret_is_a_reference_not_a_value():
    method = oidc_auth(
        discovery_url="https://idp.example.com",
        client_id="ladon",
        client_secret_ref="oidc/operator-client-secret",
    )
    assert method.type == "oidc"
    assert method.config["oidc_discovery_url"] == "https://idp.example.com"
    assert method.config["oidc_client_id"] == "ladon"
    # Reference only: the actual secret never appears in rendered config.
    assert method.config["oidc_client_secret_ref"] == "oidc/operator-client-secret"
    assert "oidc_client_secret" not in method.config


def test_oidc_auth_default_role_optional():
    method = oidc_auth(
        discovery_url="https://idp.example.com",
        client_id="ladon",
        client_secret_ref="ref",
        default_role="operator",
    )
    assert method.config["default_role"] == "operator"


@pytest.mark.parametrize(
    ("url", "cid", "ref"),
    [
        ("", "ladon", "ref"),
        ("https://idp", "", "ref"),
        ("https://idp", "ladon", ""),
    ],
)
def test_oidc_auth_requires_public_fields_and_ref(url, cid, ref):
    with pytest.raises(ConfigError):
        oidc_auth(discovery_url=url, client_id=cid, client_secret_ref=ref)


def test_oidc_role_maps_groups_to_policies():
    role = oidc_role(
        "operators",
        policies=["operator"],
        allowed_redirect_uris=["https://ladon.example.com/oidc/callback"],
        bound_groups=["platform-admins"],
    )
    assert role.data["token_policies"] == ["operator"]
    assert role.data["user_claim"] == "sub"
    assert role.data["bound_claims"] == {"groups": ["platform-admins"]}


def test_oidc_role_without_bound_groups_is_not_group_restricted():
    role = oidc_role(
        "any",
        policies=["operator"],
        allowed_redirect_uris=["https://ladon.example.com/oidc/callback"],
    )
    assert "bound_claims" not in role.data


@pytest.mark.parametrize(
    ("policies", "uris"),
    [
        ([], ["https://r"]),
        (["p"], []),
    ],
)
def test_oidc_role_rejects_empty_policies_or_redirects(policies, uris):
    with pytest.raises(ConfigError):
        oidc_role("r", policies=policies, allowed_redirect_uris=uris)
