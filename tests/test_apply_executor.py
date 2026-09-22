"""Tests for the apply executor (D3b, the thin SDK wrapper).

The dry-run path needs no client and touches nothing. The apply path is exercised
with a recording fake that mimics the hvac call surface, so these run with no live
OpenBao. Convergence against live state has its own file
(``test_apply_converge.py``); these cover dispatch, ordering, and error context.
"""

import pytest

from fake_openbao import FakeOpenBao as FakeClient
from ladon.apply.executor import (
    ApplyError,
    apply_plan,
    supported_kinds,
)
from ladon.apply.plan import Plan, WriteOp, root_config_plan
from ladon.config.auth import jwt_auth, jwt_role, oidc_auth, oidc_role
from ladon.config.policies import external_secrets_read, team_readwrite


def sample_plan():
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
    return root_config_plan(
        policies=[team_readwrite(), external_secrets_read()],
        auth_methods=[jwt, oidc],
    )


def test_dry_run_needs_no_client_and_touches_nothing():
    result = apply_plan(sample_plan())
    assert result.dry_run is True
    assert result.applied == ()
    assert result.planned[0] == "enable-secrets-engine sys/mounts/kv"
    assert len(result.planned) == len(sample_plan())


def test_dry_run_render_is_value_free():
    # The OIDC client-secret reference must not surface in any planned summary.
    result = apply_plan(sample_plan())
    assert all("secret/oidc-client" not in line for line in result.planned)


def test_apply_dispatches_each_op_in_order():
    client = FakeClient()
    plan = sample_plan()
    result = apply_plan(plan, client=client, apply=True)

    assert result.dry_run is False
    assert result.planned == ()
    assert result.applied == tuple(op.summary() for op in plan.ops)

    # First call is the KV engine; verb sequence matches the plan's kinds.
    verbs = [c[0] for c in client.calls]
    assert verbs[0] == "enable_secrets_engine"
    assert verbs.count("create_or_update_policy") == 2
    assert verbs.count("enable_auth_method") == 2


def test_apply_passes_through_payloads_to_correct_calls():
    client = FakeClient()
    apply_plan(sample_plan(), client=client, apply=True)

    # KV engine enabled at mount "kv" with v2 options.
    engine = next(c for c in client.calls if c[0] == "enable_secrets_engine")
    assert engine[1] == "kv"
    assert engine[2] == "kv"
    assert engine[3] == {"version": "2"}

    # Auth config and role go through write_data at their API paths.
    writes = {c[1]: c[2] for c in client.calls if c[0] == "write_data"}
    assert "auth/jwt/config" in writes
    assert "auth/jwt/role/eso" in writes
    assert writes["auth/jwt/role/eso"]["bound_subject"] == "system:serviceaccount:apps:eso"


def test_apply_requires_client():
    with pytest.raises(ApplyError, match="requires an authenticated hvac client"):
        apply_plan(sample_plan(), apply=True)


def test_unknown_op_kind_fails_before_touching_openbao():
    plan = Plan(ops=(WriteOp(kind="frobnicate", path="sys/wat"),))
    client = FakeClient()
    with pytest.raises(ApplyError, match="no handler for op kind"):
        apply_plan(plan, client=client, apply=True)
    assert client.calls == []


def test_unknown_op_kind_fails_dry_run_too():
    # An unexecutable plan is rejected even on a dry-run, so a plan that could
    # never apply does not look reviewable-and-safe.
    plan = Plan(ops=(WriteOp(kind="frobnicate", path="sys/wat"),))
    with pytest.raises(ApplyError, match="no handler for op kind"):
        apply_plan(plan)


def test_apply_wraps_client_errors_with_op_context():
    class Boom(FakeClient):
        def write_data(self, path, *, data):
            raise RuntimeError("connection refused")

    plan = sample_plan()
    with pytest.raises(ApplyError, match="failed to apply configure-auth-method"):
        apply_plan(plan, client=Boom(), apply=True)


def test_supported_kinds_covers_root_config_plan():
    kinds = {op.kind for op in sample_plan().ops}
    assert kinds <= supported_kinds()
