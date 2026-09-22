"""Tests for apply convergence (D3b): every op is a desired state, not a write.

The design promises a dry-run that diffs against live state and an apply that
converges to it. These cover the four cases that matter operationally: a fresh
install, a second run (which must write nothing), drift in one object, and a
write that does not land.
"""

import pytest

from fake_openbao import FakeOpenBao
from ladon.apply.executor import ApplyError, apply_plan
from ladon.apply.plan import Plan, WriteOp, root_config_plan, team_onboarding_plan
from ladon.config.auth import jwt_auth, jwt_role
from ladon.config.policies import external_secrets_read, team_readwrite

ISSUER = "https://issuer.example/.well-known/openid-configuration"


def sample_plan():
    jwt = jwt_auth(
        discovery_url=ISSUER,
        roles=[
            jwt_role(
                "eso",
                policies=["external-secrets-read"],
                bound_subject="system:serviceaccount:external-secrets:external-secrets",
                bound_audiences=["bao"],
            )
        ],
    )
    return root_config_plan(
        policies=[team_readwrite(), external_secrets_read()],
        auth_methods=[jwt],
    )


def actions(result):
    return [d.action for d in result.diffs]


# Fresh install ---------------------------------------------------------------


def test_fresh_install_creates_every_object():
    client = FakeOpenBao()
    result = apply_plan(sample_plan(), client=client, apply=True)

    assert actions(result) == ["create"] * len(sample_plan())
    assert result.applied == tuple(op.summary() for op in sample_plan().ops)
    assert len(client.calls) == len(sample_plan())


def test_fresh_install_reports_create_without_field_changes():
    client = FakeOpenBao()
    result = apply_plan(sample_plan(), client=client, apply=True)
    assert result.diffs[0].render() == "create enable-secrets-engine sys/mounts/kv"


# Second run ------------------------------------------------------------------


def test_second_run_is_already_current_and_writes_nothing():
    client = FakeOpenBao()
    apply_plan(sample_plan(), client=client, apply=True)
    writes_after_first = len(client.calls)

    result = apply_plan(sample_plan(), client=client, apply=True)

    assert actions(result) == ["already current"] * len(sample_plan())
    assert result.applied == ()
    assert len(client.calls) == writes_after_first


def test_second_run_of_the_onboarding_plan_is_already_current():
    plan = team_onboarding_plan(cluster="cluster-a", oidc_discovery_url=ISSUER)
    client = FakeOpenBao()
    apply_plan(plan, client=client, apply=True)

    result = apply_plan(plan, client=client, apply=True)
    assert actions(result) == ["already current"] * len(plan)


def test_a_role_read_back_as_seconds_and_reordered_lists_is_current():
    # OpenBao echoes a duration back as an integer number of seconds and does
    # not preserve list order, so neither is drift.
    plan = Plan(
        ops=(
            WriteOp(
                kind="write-auth-role",
                path="auth/jwt/role/eso",
                data={"token_ttl": "1h", "token_policies": ["a", "b"]},
            ),
        )
    )
    client = FakeOpenBao(
        paths={"auth/jwt/role/eso": {"token_ttl": 3600, "token_policies": ["b", "a"]}}
    )
    result = apply_plan(plan, client=client, apply=True)
    assert actions(result) == ["already current"]
    assert client.calls == []


# Drift -----------------------------------------------------------------------


def test_drifted_policy_updates_only_that_op():
    client = FakeOpenBao()
    apply_plan(sample_plan(), client=client, apply=True)
    client.policies["team-readwrite"] = 'path "kv/data/*" {\n  capabilities = ["read"]\n}\n'
    writes_after_first = len(client.calls)

    result = apply_plan(sample_plan(), client=client, apply=True)

    assert [d.action for d in result.diffs if d.summary.endswith("team-readwrite")] == ["update"]
    assert actions(result).count("update") == 1
    assert result.applied == ("write-policy sys/policies/acl/team-readwrite",)
    assert len(client.calls) == writes_after_first + 1


def test_drifted_field_is_named_in_the_diff():
    client = FakeOpenBao()
    apply_plan(sample_plan(), client=client, apply=True)
    client.paths["auth/jwt/config"]["bound_issuer"] = "https://stale.example"

    result = apply_plan(sample_plan(), client=client, apply=False)
    diff = next(d for d in result.diffs if d.summary == "configure-auth-method auth/jwt/config")
    assert diff.action == "update"
    assert diff.render() == (
        "update configure-auth-method auth/jwt/config "
        f"(bound_issuer: https://stale.example -> {ISSUER})"
    )


def test_a_missing_field_shows_as_absent():
    client = FakeOpenBao()
    apply_plan(sample_plan(), client=client, apply=True)
    del client.paths["auth/jwt/role/eso"]["token_no_default_policy"]

    result = apply_plan(sample_plan(), client=client, apply=False)
    diff = next(d for d in result.diffs if d.summary.endswith("role/eso"))
    assert "token_no_default_policy: (absent) -> True" in diff.render()


def test_a_policy_body_difference_is_reported_without_printing_it():
    # A policy body is multi-line; the diff names the field and says it differs
    # rather than dumping a payload into a review surface.
    client = FakeOpenBao()
    apply_plan(sample_plan(), client=client, apply=True)
    client.policies["team-readwrite"] = 'path "kv/data/*" {\n  capabilities = ["read"]\n}\n'

    result = apply_plan(sample_plan(), client=client, apply=False)
    diff = next(d for d in result.diffs if d.summary.endswith("team-readwrite"))
    assert diff.render() == "update write-policy sys/policies/acl/team-readwrite (policy: differs)"


def test_whitespace_only_policy_difference_is_not_drift():
    client = FakeOpenBao()
    apply_plan(sample_plan(), client=client, apply=True)
    client.policies["team-readwrite"] = "\n\n" + client.policies["team-readwrite"].replace(
        "\n", "  \n"
    )

    result = apply_plan(sample_plan(), client=client, apply=False)
    diff = next(d for d in result.diffs if d.summary.endswith("team-readwrite"))
    assert diff.action == "already current"


# Connected dry-run -----------------------------------------------------------


def test_connected_dry_run_diffs_without_writing():
    client = FakeOpenBao()
    result = apply_plan(sample_plan(), client=client, apply=False)

    assert result.dry_run is True
    assert result.applied == ()
    assert actions(result) == ["create"] * len(sample_plan())
    assert client.calls == []
    assert client.reads != []


def test_offline_dry_run_still_needs_no_client():
    result = apply_plan(sample_plan())
    assert result.dry_run is True
    assert result.diffs == ()
    assert result.planned[0] == "enable-secrets-engine sys/mounts/kv"


# Read-back verification ------------------------------------------------------


def test_read_back_mismatch_names_the_op_and_the_field():
    class DropsAField(FakeOpenBao):
        def write_data(self, path, *, data):
            super().write_data(path, data={k: v for k, v in data.items() if k != "bound_issuer"})

    with pytest.raises(ApplyError, match="configure-auth-method auth/jwt/config"):
        apply_plan(sample_plan(), client=DropsAField(), apply=True)

    with pytest.raises(ApplyError, match="bound_issuer"):
        apply_plan(sample_plan(), client=DropsAField(), apply=True)


def test_read_back_of_a_vanished_object_is_an_error():
    class DiscardsPolicies(FakeOpenBao):
        def __init__(self):
            super().__init__()
            self.sys.create_or_update_policy = self._swallow

        def _swallow(self, *, name, policy):
            self.calls.append(("create_or_update_policy", name, policy))

    with pytest.raises(ApplyError, match="write-policy sys/policies/acl/team-readwrite"):
        apply_plan(sample_plan(), client=DiscardsPolicies(), apply=True)


# Objects that cannot be converged in place -----------------------------------


def test_a_mount_of_the_wrong_type_refuses_rather_than_destroying_data():
    client = FakeOpenBao(mounts={"kv/": {"type": "cubbyhole", "options": {}}})
    with pytest.raises(ApplyError, match="cannot converge enable-secrets-engine"):
        apply_plan(sample_plan(), client=client, apply=True)
    assert client.calls == []


def test_a_mount_option_difference_is_tuned_in_place():
    client = FakeOpenBao(mounts={"kv/": {"type": "kv", "options": {"version": "1"}}})
    result = apply_plan(sample_plan(), client=client, apply=True)

    assert result.diffs[0].action == "update"
    assert client.calls[0] == ("tune_mount_configuration", "kv", {"version": "2"})


def test_an_auth_mount_of_the_wrong_type_refuses():
    client = FakeOpenBao(auth_methods={"jwt/": {"type": "oidc"}})
    with pytest.raises(ApplyError, match="cannot converge enable-auth-method"):
        apply_plan(sample_plan(), client=client, apply=False)
