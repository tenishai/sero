"""Tests for the phase-10 team-onboarding plan (D3b).

These cover the op shape and ordering, faithfulness to the source convention
(jwt-<cluster> mount, audience-bound open role, ESO read policy), the value-free
render contract, and that the plan is executable by the existing executor.
"""

import pytest

from ladon.apply.executor import supported_kinds
from ladon.apply.plan import (
    DEFAULT_ESO_AUDIENCE,
    DEFAULT_ESO_POLICY,
    DEFAULT_ESO_ROLE,
    team_onboarding_plan,
)
from ladon.config.policies import external_secrets_read
from ladon.config.teams import ConfigError

ISSUER = "https://container.example/v1/clusters/team-a"


def test_onboarding_op_order_and_paths():
    plan = team_onboarding_plan(cluster="team-a", oidc_discovery_url=ISSUER)
    assert [o.kind for o in plan.ops] == [
        "enable-auth-method",
        "configure-auth-method",
        "write-policy",
        "write-auth-role",
    ]
    paths = [o.path for o in plan.ops]
    assert paths == [
        "sys/auth/jwt-team-a",
        "auth/jwt-team-a/config",
        f"sys/policies/acl/{DEFAULT_ESO_POLICY}",
        f"auth/jwt-team-a/role/{DEFAULT_ESO_ROLE}",
    ]


def test_policy_written_before_role_that_references_it():
    plan = team_onboarding_plan(cluster="team-a", oidc_discovery_url=ISSUER)
    kinds = [o.kind for o in plan.ops]
    assert kinds.index("write-policy") < kinds.index("write-auth-role")


def test_oidc_config_uses_discovery_url_as_bound_issuer_and_default_role():
    plan = team_onboarding_plan(cluster="team-a", oidc_discovery_url=ISSUER)
    config = next(o for o in plan.ops if o.kind == "configure-auth-method").data
    assert config["oidc_discovery_url"] == ISSUER
    assert config["bound_issuer"] == ISSUER
    assert config["default_role"] == DEFAULT_ESO_ROLE


def test_role_is_bound_to_the_eso_service_account_and_the_policy():
    plan = team_onboarding_plan(cluster="team-a", oidc_discovery_url=ISSUER)
    role = next(o for o in plan.ops if o.kind == "write-auth-role").data
    assert role["role_type"] == "jwt"
    assert role["bound_audiences"] == [DEFAULT_ESO_AUDIENCE]
    assert role["token_policies"] == [DEFAULT_ESO_POLICY]
    assert role["user_claim"] == "sub"
    # The audience alone is forgeable by any workload in the cluster that can
    # mint a token for it, so the subject pins the role to the ESO identity.
    assert role["bound_subject"] == "system:serviceaccount:external-secrets:external-secrets"
    assert role["token_no_default_policy"] is True


def test_eso_namespace_is_a_parameter():
    plan = team_onboarding_plan(
        cluster="team-a",
        oidc_discovery_url=ISSUER,
        eso_namespace="secrets-operator",
    )
    role = next(o for o in plan.ops if o.kind == "write-auth-role").data
    assert role["bound_subject"] == "system:serviceaccount:secrets-operator:external-secrets"


def test_policy_hcl_matches_baseline_eso_read_grant():
    plan = team_onboarding_plan(cluster="team-a", oidc_discovery_url=ISSUER)
    policy = next(o for o in plan.ops if o.kind == "write-policy").data
    assert policy["policy"] == external_secrets_read().hcl


def test_parameters_override_defaults():
    plan = team_onboarding_plan(
        cluster="prod-1",
        oidc_discovery_url=ISSUER,
        kv_mount="team-kv",
        policy_name="eso-ro",
        role_name="eso",
        audience="ladon",
    )
    assert plan.ops[0].path == "sys/auth/jwt-prod-1"
    role = next(o for o in plan.ops if o.kind == "write-auth-role").data
    assert role["bound_audiences"] == ["ladon"]
    assert role["token_policies"] == ["eso-ro"]
    policy = next(o for o in plan.ops if o.kind == "write-policy")
    assert policy.path == "sys/policies/acl/eso-ro"
    assert "team-kv/data/*" in policy.data["policy"]


def test_empty_cluster_rejected():
    with pytest.raises(ConfigError, match="non-empty 'cluster'"):
        team_onboarding_plan(cluster="", oidc_discovery_url=ISSUER)


def test_render_is_value_free():
    plan = team_onboarding_plan(cluster="team-a", oidc_discovery_url=ISSUER)
    rendered = plan.render()
    assert len(rendered.splitlines()) == len(plan)
    # The discovery URL is config, not a secret, but the render still shows only
    # kinds and paths, never payloads.
    assert ISSUER not in rendered
    assert rendered.splitlines()[0] == "enable-auth-method sys/auth/jwt-team-a"


def test_plan_is_executable_by_the_executor():
    plan = team_onboarding_plan(cluster="team-a", oidc_discovery_url=ISSUER)
    assert {o.kind for o in plan.ops} <= supported_kinds()
