"""Tests for apply convergence (D3b): every op is a desired state, not a write.

The design promises a dry-run that diffs against live state and an apply that
converges to it. These cover the four cases that matter operationally: a fresh
install, a second run (which must write nothing), drift in one object, and a
write that does not land.
"""

import pytest

from fake_openbao import FakeOpenBao
from sero.apply.executor import ApplyError, apply_plan
from sero.apply.plan import (
    Plan,
    WriteOp,
    app_onboarding_plan,
    root_config_plan,
    team_onboarding_plan,
)
from sero.config.apps import app_identity
from sero.config.auth import jwt_auth, jwt_role, oidc_auth, oidc_role
from sero.config.policies import external_secrets_read, team_readwrite
from sero.config.teams import Team

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


def plan_with_oidc():
    """A root plan including OIDC, whose config carries a Sero-only reference."""
    return root_config_plan(
        policies=[team_readwrite()],
        auth_methods=[
            oidc_auth(
                discovery_url="https://idp.example/.well-known/openid-configuration",
                client_id="sero",
                client_secret_ref="oidc/operator-client-secret",
                roles=[
                    oidc_role(
                        "operators",
                        policies=["team-readwrite"],
                        allowed_redirect_uris=["https://bao.example/ui/callback"],
                    )
                ],
            )
        ],
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


# Fields OpenBao never stores -------------------------------------------------


class IgnoresUnknownFields(FakeOpenBao):
    """OpenBao keeps only the fields a mount defines and ignores the rest.

    ``oidc_client_secret_ref`` is Sero's own reference, resolved at execution
    and never stored, so no read of the mount can ever return it.
    """

    IGNORED = frozenset({"oidc_client_secret_ref"})

    def write_data(self, path, *, data):
        kept = {k: v for k, v in data.items() if k not in self.IGNORED}
        super().write_data(path, data=kept)


def test_a_reference_openbao_never_stores_does_not_fail_the_read_back():
    client = IgnoresUnknownFields()
    result = apply_plan(plan_with_oidc(), client=client, apply=True)
    assert result.applied == tuple(op.summary() for op in plan_with_oidc().ops)


def test_a_reference_openbao_never_stores_is_not_perpetual_drift():
    client = IgnoresUnknownFields()
    apply_plan(plan_with_oidc(), client=client, apply=True)

    result = apply_plan(plan_with_oidc(), client=client, apply=False)
    assert actions(result) == ["already current"] * len(plan_with_oidc())


# Rendering values ------------------------------------------------------------


def role_diff(desired: dict, live: dict):
    plan = Plan(ops=(WriteOp(kind="write-auth-role", path="auth/jwt/role/r", data=desired),))
    client = FakeOpenBao(paths={"auth/jwt/role/r": live})
    return apply_plan(plan, client=client, apply=False).diffs[0]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("token", "s.notatoken"),
        ("api_key", "notakey"),
        ("jwt", "header.payload.signature"),
        ("unrecognised_field", "some value"),
    ],
)
def test_a_field_sero_does_not_recognise_is_never_printed(field, value):
    # The renderer allows known-safe field names rather than denying a few known
    # dangerous ones, so a field nobody thought about cannot leak its value.
    diff = role_diff({field: value}, {field: "other"})
    assert diff.render() == f"update write-auth-role auth/jwt/role/r ({field}: differs)"
    assert value not in diff.render()


def test_userinfo_is_stripped_from_a_url_that_is_shown():
    diff = role_diff(
        {"oidc_discovery_url": "https://user:pw@issuer.example/.well-known/openid-configuration"},
        {"oidc_discovery_url": "https://issuer.example"},
    )
    assert "user:pw@" not in diff.render()
    assert "https://issuer.example/.well-known/openid-configuration" in diff.render()


def test_known_safe_fields_still_show_their_values():
    diff = role_diff({"token_ttl": "2h"}, {"token_ttl": 3600})
    assert diff.render() == "update write-auth-role auth/jwt/role/r (token_ttl: 3600 -> 2h)"


# Comparing values ------------------------------------------------------------


def test_a_nested_mapping_compares_by_content():
    diff = role_diff(
        {"bound_claims": {"groups": ["a", "b"]}},
        {"bound_claims": {"groups": ["b", "a"]}},
    )
    assert diff.action == "already current"


def test_a_composite_duration_compares_by_length():
    diff = role_diff({"token_ttl": "1h30m"}, {"token_ttl": 5400})
    assert diff.action == "already current"


def test_a_list_field_read_back_as_a_single_value_is_not_drift():
    diff = role_diff({"bound_audiences": ["bao"]}, {"bound_audiences": "bao"})
    assert diff.action == "already current"


def test_a_boolean_echoed_as_a_string_is_not_drift():
    diff = role_diff({"token_no_default_policy": True}, {"token_no_default_policy": "true"})
    assert diff.action == "already current"


def test_an_empty_policy_body_is_an_update_not_a_create():
    # An existing but empty policy grants nothing. Reporting "create" would hide
    # that something emptied it.
    plan = Plan(
        ops=(
            WriteOp(
                kind="write-policy",
                path="sys/policies/acl/team-readwrite",
                data={"policy": team_readwrite().hcl},
            ),
        )
    )
    client = FakeOpenBao(policies={"team-readwrite": ""})
    result = apply_plan(plan, client=client, apply=False)
    assert result.diffs[0].action == "update"


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


def test_a_mount_of_the_wrong_type_is_blocked_rather_than_destroyed():
    client = FakeOpenBao(mounts={"kv/": {"type": "cubbyhole", "options": {}}})
    result = apply_plan(sample_plan(), client=client, apply=True)

    assert result.diffs[0].action == "blocked"
    assert "type" in result.diffs[0].render()
    assert client.calls == []


def test_a_kv_version_difference_is_blocked_rather_than_upgraded_in_place():
    # Tuning options on a live v1 mount triggers OpenBao's one-way KV upgrade,
    # which moves every secret under kv/data/*. That is an operator's decision.
    client = FakeOpenBao(mounts={"kv/": {"type": "kv", "options": {"version": "1"}}})
    result = apply_plan(sample_plan(), client=client, apply=True)

    assert result.diffs[0].action == "blocked"
    assert "version: 1 -> 2" in result.diffs[0].render()
    assert client.calls == []


def test_an_auth_mount_of_the_wrong_type_is_blocked():
    client = FakeOpenBao(auth_methods={"jwt/": {"type": "oidc"}})
    result = apply_plan(sample_plan(), client=client, apply=False)
    blocked = [d for d in result.diffs if d.action == "blocked"]
    assert [d.summary for d in blocked] == ["enable-auth-method sys/auth/jwt"]


def test_a_blocked_op_does_not_hide_the_rest_of_the_plan():
    # The report is the point of a dry-run: one op that cannot converge must not
    # truncate it.
    client = FakeOpenBao(mounts={"kv/": {"type": "cubbyhole", "options": {}}})
    result = apply_plan(sample_plan(), client=client, apply=False)

    assert len(result.diffs) == len(sample_plan())
    assert result.diffs[0].action == "blocked"
    assert [d.action for d in result.diffs[1:]] == ["create"] * (len(sample_plan()) - 1)


def test_a_block_anywhere_stops_the_whole_apply_before_any_write():
    # The mount is fine; the auth method is not. Nothing may be written, so a
    # half-applied plan cannot exist.
    client = FakeOpenBao(auth_methods={"jwt/": {"type": "oidc"}})
    result = apply_plan(sample_plan(), client=client, apply=True)

    assert client.calls == []
    assert result.applied == ()
    assert [d.summary for d in result.blocked] == ["enable-auth-method sys/auth/jwt"]


# Namespaces ------------------------------------------------------------------


def team_plan():
    return root_config_plan(
        policies=[],
        auth_methods=[],
        teams=[
            Team(name="team-a", namespace="team-a", environments=("dev",)),
            Team(name="team-b", namespace="team-b", environments=("dev",)),
        ],
    )


def test_a_namespace_is_created_when_absent_and_current_afterwards():
    client = FakeOpenBao()
    result = apply_plan(team_plan(), client=client, apply=True)

    assert actions(result)[1] == "create"
    assert client.namespaces == {"team-a", "team-b"}

    again = apply_plan(team_plan(), client=client, apply=True)
    assert actions(again) == ["already current"] * len(team_plan())
    assert again.applied == ()


def test_an_existing_namespace_is_never_recreated():
    client = FakeOpenBao(namespaces={"team-a"})
    result = apply_plan(team_plan(), client=client, apply=True)

    created = [c for c in client.calls if c[0] == "create_namespace"]
    assert [c[1] for c in created] == ["team-b"]
    assert result.diffs[1].action == "already current"


def test_every_namespaced_read_and_write_carries_the_namespace():
    client = FakeOpenBao()
    apply_plan(team_plan(), client=client, apply=True)

    # The namespace is the last element of each recorded read and write.
    assert ("create_namespace", "team-a", None) in client.calls
    assert ("enable_secrets_engine", "kv", "kv", {"version": "2"}, "team-a") in client.calls
    assert ("list_mounted_secrets_engines", "team-a") in client.reads
    assert [c for c in client.calls if c[-1] == "team-b"]


def test_the_namespace_header_is_restored_after_each_op():
    client = FakeOpenBao()
    apply_plan(team_plan(), client=client, apply=True)
    assert client.adapter.namespace is None


def test_the_same_mount_in_two_namespaces_is_two_objects():
    # Without per-namespace state a second team would read as already current
    # and never get its own KV mount.
    client = FakeOpenBao()
    apply_plan(team_plan(), client=client, apply=True)

    mounts = [c for c in client.calls if c[0] == "enable_secrets_engine"]
    assert [c[-1] for c in mounts] == [None, "team-a", "team-b"]


def test_a_namespaced_policy_drift_is_repaired_in_its_own_namespace():
    client = FakeOpenBao()
    apply_plan(team_plan(), client=client, apply=True)
    client.state("team-b").policies["team-readwrite"] = 'path "kv/data/*" {\n}\n'

    result = apply_plan(team_plan(), client=client, apply=True)
    assert result.applied == ("[team-b] write-policy sys/policies/acl/team-readwrite",)


def test_a_team_never_reads_or_writes_the_roots_objects():
    # The root already has a KV mount at the same path and the same policy
    # names, with different bodies. Every read, write and read-back of a team's
    # op must happen inside the team's namespace: an unscoped read would report
    # the team's mount already current and its policies as drift against the
    # root's, and an unscoped read-back would fail on bodies that never match.
    root_body = 'path "kv/data/*" {\n  capabilities = ["read"]\n}\n'
    client = FakeOpenBao(
        mounts={"kv/": {"type": "kv", "options": {"version": "2"}}},
        policies={"team-readwrite": root_body, "external-secrets-read": root_body},
    )

    result = apply_plan(team_plan(), client=client, apply=True)

    assert [d.action for d in result.diffs if d.summary.startswith("[")] == ["create"] * 6
    for namespace in ("team-a", "team-b"):
        state = client.state(namespace)
        assert "kv/" in state.mounts
        assert sorted(state.policies) == ["external-secrets-read", "team-readwrite"]
        assert state.policies["team-readwrite"] != root_body
    # The root keeps exactly what it was seeded with.
    assert client.policies == {"team-readwrite": root_body, "external-secrets-read": root_body}
    assert list(client.mounts) == ["kv/"]


def test_a_client_that_cannot_scope_a_namespace_is_refused():
    class Unscopable:
        """A hand-rolled client with no namespace header to set."""

    plan = team_onboarding_plan(cluster="cluster-a", oidc_discovery_url=ISSUER, namespace="team-a")
    # Refused before the first request, rather than quietly writing to the root.
    with pytest.raises(ApplyError, match="namespace"):
        apply_plan(plan, client=Unscopable(), apply=True)


def test_an_op_namespace_nests_under_the_clients_own():
    # X-Vault-Namespace is a path: a client already scoped to a parent runs the
    # op in the child below it, not in a sibling of the parent.
    client = FakeOpenBao(namespace="platform")
    plan = team_onboarding_plan(cluster="cluster-a", oidc_discovery_url=ISSUER, namespace="team-a")
    apply_plan(plan, client=client, apply=True)

    assert {c[-1] for c in client.calls} == {"platform/team-a"}
    assert client.adapter.namespace == "platform"


# App onboarding --------------------------------------------------------------


def app_plan():
    return app_onboarding_plan(app_identity(team="team-a", app="sample", cluster="cluster-a"))


def test_app_onboarding_converges_inside_the_team_namespace():
    client = FakeOpenBao()
    result = apply_plan(app_plan(), client=client, apply=True)

    assert actions(result) == ["create", "create"]
    assert "eso-sample-readonly" in client.state("team-a").policies
    assert {c[-1] for c in client.calls} == {"team-a"}

    again = apply_plan(app_plan(), client=client, apply=True)
    assert actions(again) == ["already current", "already current"]
