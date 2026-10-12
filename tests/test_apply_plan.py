"""Tests for the pure apply-plan layer (D3b).

These cover op construction, ordering, the value-free summary/render contract,
and duplicate rejection, with no live OpenBao required.
"""

import pytest

from sero.apply.plan import (
    KV_VERSION,
    Plan,
    WriteOp,
    auth_method_ops,
    ensure_namespace_op,
    kv_mount_ops,
    policy_ops,
    root_config_plan,
)
from sero.config.auth import jwt_auth, jwt_role, oidc_auth, oidc_role
from sero.config.policies import (
    external_secrets_read,
    team_readonly,
    team_readwrite,
)
from sero.config.teams import ConfigError, Team


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
        client_id="sero",
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
        data={"oidc_client_secret_ref": "secret/oidc-client", "oidc_client_id": "sero"},
    )
    summary = op.summary()
    assert summary == "configure-auth-method auth/oidc/config"
    assert "secret/oidc-client" not in summary
    assert "sero" not in summary


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


# Namespaces ------------------------------------------------------------------


def teams():
    return [
        Team(name="team-a", namespace="team-a", environments=("dev",)),
        Team(name="team-b", namespace="ns-b", environments=("dev",), access="readonly"),
    ]


def test_ensure_namespace_op_targets_the_namespaces_own_path():
    op = ensure_namespace_op("team-a")
    assert op.kind == "ensure-namespace"
    assert op.path == "sys/namespaces/team-a"
    # Created from the parent it lives under, so the op itself is unscoped.
    assert op.namespace is None
    assert op.summary() == "ensure-namespace sys/namespaces/team-a"


def test_ensure_namespace_refuses_a_path():
    # A nested namespace is created by running this op inside its parent, not by
    # posting a path from the root.
    with pytest.raises(ConfigError, match="single segment"):
        ensure_namespace_op("platform/team-a")


def test_a_namespaced_op_shows_its_namespace_in_the_summary():
    op = WriteOp(kind="enable-secrets-engine", path="sys/mounts/kv", namespace="team-a")
    assert op.summary() == "[team-a] enable-secrets-engine sys/mounts/kv"


def test_root_config_creates_each_team_namespace_and_its_contents():
    plan = root_config_plan(policies=[], auth_methods=[], teams=teams())
    summaries = [op.summary() for op in plan.ops]
    assert summaries[1:] == [
        "ensure-namespace sys/namespaces/team-a",
        "[team-a] enable-secrets-engine sys/mounts/kv",
        "[team-a] write-policy sys/policies/acl/team-readwrite",
        "[team-a] write-policy sys/policies/acl/external-secrets-read",
        "ensure-namespace sys/namespaces/ns-b",
        "[ns-b] enable-secrets-engine sys/mounts/kv",
        "[ns-b] write-policy sys/policies/acl/team-readonly",
        "[ns-b] write-policy sys/policies/acl/external-secrets-read",
    ]


def test_a_team_gets_only_the_policies_its_access_level_grants():
    # A read-only team has no business holding a write policy, even an unbound
    # one: a policy that exists is a policy a role can be pointed at later.
    plan = root_config_plan(policies=[], auth_methods=[], teams=teams())
    written = {op.namespace: [] for op in plan.ops if op.namespace}
    for op in plan.ops:
        if op.kind == "write-policy" and op.namespace:
            written[op.namespace].append(op.path.rsplit("/", 1)[-1])
    assert written["team-a"] == ["team-readwrite", "external-secrets-read"]
    assert written["ns-b"] == ["team-readonly", "external-secrets-read"]


def test_the_namespace_is_created_before_anything_is_mounted_in_it():
    plan = root_config_plan(policies=[], auth_methods=[], teams=teams())
    kinds = [op.kind for op in plan.ops]
    first_team_op = kinds.index("ensure-namespace")
    assert all(op.namespace is None for op in plan.ops[:first_team_op])
    assert plan.ops[first_team_op + 1].namespace == "team-a"


def test_root_objects_are_unchanged_by_teams():
    without = root_config_plan(policies=[team_readwrite()], auth_methods=sample_auth_methods())
    with_teams = root_config_plan(
        policies=[team_readwrite()], auth_methods=sample_auth_methods(), teams=teams()
    )
    assert with_teams.ops[: len(without)] == without.ops


def test_team_policies_follow_the_deployments_kv_mount():
    plan = root_config_plan(policies=[], auth_methods=[], teams=teams(), kv_mount="team-kv")
    team_ops = [op for op in plan.ops if op.namespace == "team-a"]
    assert team_ops[0].path == "sys/mounts/team-kv"
    assert "team-kv/data/*" in team_ops[1].data["policy"]


def test_the_same_policy_name_in_two_namespaces_is_not_a_duplicate():
    # Duplicates collide only within one namespace; team-readwrite exists once
    # per team by design.
    plan = root_config_plan(policies=[team_readwrite()], auth_methods=[], teams=teams())
    # The root mount and its one policy, then four ops for each of two teams.
    assert len(plan) == 2 + 2 * 4


def test_a_policy_name_repeated_in_one_namespace_is_still_rejected():
    with pytest.raises(ConfigError, match="duplicate policy name"):
        root_config_plan(policies=[team_readonly(), team_readonly()], auth_methods=[])


def test_only_teams_plans_one_teams_namespace_and_nothing_at_the_root():
    # What an approved namespace request applies. Approving one team's request
    # must not rewrite the deployment's auth methods.
    plan = root_config_plan(
        policies=[team_readwrite()],
        auth_methods=sample_auth_methods(),
        teams=teams(),
        only_teams=["team-b"],
    )
    assert [op.summary() for op in plan.ops] == [
        "ensure-namespace sys/namespaces/ns-b",
        "[ns-b] enable-secrets-engine sys/mounts/kv",
        "[ns-b] write-policy sys/policies/acl/team-readonly",
        "[ns-b] write-policy sys/policies/acl/external-secrets-read",
    ]


def test_only_teams_refuses_a_team_the_config_does_not_define():
    # The namespace, the mount and the policies all come from the config entry;
    # with no entry there is nothing to apply, and guessing would create a
    # namespace nobody configured.
    with pytest.raises(ConfigError, match="team-c"):
        root_config_plan(policies=[], auth_methods=[], teams=teams(), only_teams=["team-c"])


def test_no_only_teams_filter_plans_everything():
    everything = root_config_plan(policies=[], auth_methods=[], teams=teams())
    assert root_config_plan(policies=[], auth_methods=[], teams=teams(), only_teams=None).ops == (
        everything.ops
    )
