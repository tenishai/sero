"""Tests for the pure apply-plan layer (D3b).

These cover op construction, ordering, the value-free summary/render contract,
and duplicate rejection, with no live OpenBao required.
"""

import pytest

from ladon.apply.plan import (
    KV_VERSION,
    Plan,
    WriteOp,
    auth_method_ops,
    kv_mount_ops,
    policy_ops,
    root_config_plan,
)
from ladon.config.auth import jwt_auth, jwt_role, oidc_auth, oidc_role
from ladon.config.policies import (
    external_secrets_read,
    team_readonly,
    team_readwrite,
)
from ladon.config.teams import ConfigError


def sample_auth_methods():
    jwt = jwt_auth(
        discovery_url="https://issuer.example/.well-known/openid-configuration",
        roles=[
            jwt_role(
                "eso",
                policies=["external-secrets-read"],
                bound_subject="system:serviceaccount:apps:eso",
            )
        ],
    )
    oidc = oidc_auth(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        client_id="ladon",
        client_secret_ref="secret/oidc-client",
        roles=[
            oidc_role(
                "operators",
                policies=["team-readwrite"],
                allowed_redirect_uris=["https://bao.example/ui/callback"],
            )
        ],
    )
    return [jwt, oidc]


def test_write_op_summary_is_value_free():
    op = WriteOp(
        kind="configure-auth-method",
        path="auth/oidc/config",
        data={"oidc_client_secret_ref": "secret/oidc-client", "oidc_client_id": "ladon"},
    )
    summary = op.summary()
    assert summary == "configure-auth-method auth/oidc/config"
    assert "secret/oidc-client" not in summary
    assert "ladon" not in summary


def test_kv_mount_op_pins_v2():
    ops = kv_mount_ops("kv")
    assert len(ops) == 1
    assert ops[0].kind == "enable-secrets-engine"
    assert ops[0].path == "sys/mounts/kv"
    assert ops[0].data == {"type": "kv", "options": {"version": KV_VERSION}}


def test_policy_ops_one_per_policy():
    policies = [team_readwrite(), team_readonly(), external_secrets_read()]
    ops = policy_ops(policies)
    assert [o.path for o in ops] == [
        "sys/policies/acl/team-readwrite",
        "sys/policies/acl/team-readonly",
        "sys/policies/acl/external-secrets-read",
    ]
    assert all(o.kind == "write-policy" for o in ops)
    assert ops[0].data["policy"] == team_readwrite().hcl


def test_auth_method_ops_order_enable_config_then_roles():
    jwt = jwt_auth(
        discovery_url="https://issuer.example/.well-known/openid-configuration",
        roles=[
            jwt_role(
                "eso",
                policies=["external-secrets-read"],
                bound_subject="system:serviceaccount:apps:eso",
            )
        ],
    )
    ops = auth_method_ops(jwt)
    assert [o.kind for o in ops] == [
        "enable-auth-method",
        "configure-auth-method",
        "write-auth-role",
    ]
    assert ops[0].path == "sys/auth/jwt"
    assert ops[1].path == "auth/jwt/config"
    assert ops[2].path == "auth/jwt/role/eso"


def test_auth_method_with_no_roles_emits_enable_and_config_only():
    jwt = jwt_auth(discovery_url="https://issuer.example/.well-known/openid-configuration")
    ops = auth_method_ops(jwt)
    assert [o.kind for o in ops] == ["enable-auth-method", "configure-auth-method"]


def test_root_config_plan_full_ordering():
    policies = [team_readwrite(), team_readonly(), external_secrets_read()]
    plan = root_config_plan(policies=policies, auth_methods=sample_auth_methods())
    kinds = [o.kind for o in plan.ops]
    # KV first, then all policies, then each auth method's enable/config/role.
    assert kinds[0] == "enable-secrets-engine"
    assert kinds[1:4] == ["write-policy", "write-policy", "write-policy"]
    assert kinds[4:7] == ["enable-auth-method", "configure-auth-method", "write-auth-role"]
    assert kinds[7:10] == ["enable-auth-method", "configure-auth-method", "write-auth-role"]


def test_root_config_plan_rejects_duplicate_policy_names():
    with pytest.raises(ConfigError, match="duplicate policy name"):
        root_config_plan(policies=[team_readwrite(), team_readwrite()], auth_methods=[])


def test_root_config_plan_rejects_duplicate_auth_paths():
    a = jwt_auth(discovery_url="https://a.example/.well-known/openid-configuration", path="jwt")
    b = jwt_auth(discovery_url="https://b.example/.well-known/openid-configuration", path="jwt")
    with pytest.raises(ConfigError, match="duplicate auth mount path"):
        root_config_plan(policies=[], auth_methods=[a, b])


def test_root_config_plan_honors_custom_kv_mount():
    plan = root_config_plan(policies=[], auth_methods=[], kv_mount="team-kv")
    assert plan.ops[0].path == "sys/mounts/team-kv"


def test_plan_render_is_value_free_and_one_line_per_op():
    plan = root_config_plan(policies=[team_readwrite()], auth_methods=sample_auth_methods())
    rendered = plan.render()
    assert len(rendered.splitlines()) == len(plan)
    # No payload content (a secret reference) leaks into the rendered plan.
    assert "secret/oidc-client" not in rendered
    assert rendered.splitlines()[0] == "enable-secrets-engine sys/mounts/kv"


def test_empty_plan_renders_explanation():
    assert Plan().render() == "(empty plan: no operations)"
    assert len(Plan()) == 0
