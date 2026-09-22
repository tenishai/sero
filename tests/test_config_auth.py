"""Tests for the baseline auth-method rendering (JWT workloads + OIDC humans)."""

import pytest

from ladon.config import (
    ConfigError,
    jwt_auth,
    jwt_role,
    oidc_auth,
    oidc_role,
)


def test_jwt_auth_minimal_needs_no_secret():
    method = jwt_auth(discovery_url="https://kube.example.com")
    assert method.type == "jwt"
    assert method.path == "jwt"
    assert method.roles == ()
    # JWT verifies via the provider's published keys: no secret is configured.
    assert not any("secret" in k for k in method.config)


def test_jwt_auth_bounds_the_issuer_to_the_discovery_url_by_default():
    # An unbound issuer accepts any token whose signature the discovery keys
    # verify, so the default must pin it.
    method = jwt_auth(discovery_url="https://kube.example.com")
    assert method.config == {
        "oidc_discovery_url": "https://kube.example.com",
        "bound_issuer": "https://kube.example.com",
    }


def test_jwt_auth_bound_issuer_can_differ_from_the_discovery_url():
    method = jwt_auth(
        discovery_url="https://kube.example.com/.well-known/openid-configuration",
        bound_issuer="https://kube.example.com",
    )
    assert method.config["bound_issuer"] == "https://kube.example.com"


def test_jwt_auth_optional_issuer_and_default_role():
    method = jwt_auth(
        discovery_url="https://kube.example.com",
        bound_issuer="https://kube.example.com",
        default_role="external-secrets",
    )
    assert method.config["bound_issuer"] == "https://kube.example.com"
    assert method.config["default_role"] == "external-secrets"


def test_jwt_auth_requires_discovery_url():
    with pytest.raises(ConfigError, match="discovery_url"):
        jwt_auth(discovery_url="")


def test_jwt_role_binds_eso_service_account_subject():
    role = jwt_role(
        "external-secrets",
        policies=["external-secrets-readonly"],
        bound_subject="system:serviceaccount:external-secrets:external-secrets",
        bound_audiences=["bao"],
    )
    assert role.data["role_type"] == "jwt"
    assert role.data["token_policies"] == ["external-secrets-readonly"]
    assert role.data["bound_subject"] == ("system:serviceaccount:external-secrets:external-secrets")
    assert role.data["bound_audiences"] == ["bao"]
    assert role.data["token_ttl"] == "1h"


def test_jwt_role_never_inherits_the_namespace_default_policy():
    # The default policy is not narrow and grows over time; a scoped role that
    # inherits it is decorative.
    role = jwt_role("eso", policies=["p"], bound_audiences=["bao"])
    assert role.data["token_no_default_policy"] is True


def test_jwt_role_requires_a_binding():
    # No bound_subject and no bound_audiences: must fail closed.
    with pytest.raises(ConfigError, match="unbound role"):
        jwt_role("open", policies=["p"])


def test_jwt_role_requires_policies():
    with pytest.raises(ConfigError, match="policies"):
        jwt_role("r", policies=[], bound_subject="system:serviceaccount:ns:sa")


def test_jwt_auth_carries_roles():
    role = jwt_role("eso", policies=["p"], bound_subject="system:serviceaccount:ns:sa")
    method = jwt_auth(discovery_url="https://k", roles=[role])
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
