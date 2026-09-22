"""Tests for the role and policy audit (Task 8).

Everything here runs against the recording fake, so the audit is exercised
against real response shapes without a live OpenBao. The fixtures build two
kinds of namespace: one Ladon itself rendered (which must audit clean) and one
that has been widened by hand in the ways the sibling platform was, which must
be reported.

The last test in the file is the one that keeps the rest honest: it applies the
real plans (root config, team onboarding, three application onboardings) to the
fake and asserts that what Ladon writes audits with no findings at all. A check
that fires on Ladon's own output is a check nobody will keep.

No fixture carries a secret value: a policy body is a set of paths and
capabilities, and a role is a set of bindings.
"""

import pytest

from fake_openbao import FakeOpenBao
from ladon.apply import audit as audit_module
from ladon.apply.audit import (
    HIGH,
    LOW,
    MEDIUM,
    AuditError,
    PolicyParseError,
    audit_all,
    audit_namespace,
    parse_policy,
)
from ladon.apply.executor import apply_plan
from ladon.apply.plan import app_onboarding_plan, root_config_plan, team_onboarding_plan
from ladon.config.apps import app_identity, eso_app_policy, eso_app_role
from ladon.config.auth import jwt_auth, jwt_role, oidc_auth, oidc_role
from ladon.config.policies import baseline_team_policies, external_secrets_read
from ladon.config.teams import Team

TEAM = "team-a"
APP = "sample"
CLUSTER = "cluster-a"
NAMESPACE = "ns-a"
MOUNT = f"jwt-{CLUSTER}"
ISSUER = "https://issuer.example/.well-known/openid-configuration"

# The stock OpenBao default policy, trimmed to the shapes that matter here: it
# grants the token its own lifecycle and nothing else.
STOCK_DEFAULT = """
# Allow a token to look up its own properties.
path "auth/token/lookup-self" {
  capabilities = ["read"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}

path "auth/token/revoke-self" {
  capabilities = ["update"]
}

path "sys/capabilities-self" {
  capabilities = ["update"]
}

path "sys/tools/hash/*" {
  capabilities = ["update"]
}
"""

# What the sibling platform found months late: the default policy widened to
# full KV access plus sudo on the policy endpoints, which makes every role that
# inherits it unlimited and every scoped policy decorative.
WIDENED_DEFAULT = """
path "auth/token/lookup-self" {
  capabilities = ["read"]
}

path "kv/data/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}

path "sys/policies/acl/*" {
  capabilities = ["create", "read", "update", "delete", "list", "sudo"]
}
"""


def identity(app=APP, team=TEAM):
    return app_identity(team=team, app=app, cluster=CLUSTER, namespace=NAMESPACE)


def build(*, policies=None, roles=None, mounts=None, auth_methods=None, namespace=NAMESPACE):
    """A fake OpenBao holding one namespace's live state."""
    bao = FakeOpenBao()
    state = bao.state(namespace)
    state.mounts.update(mounts or {"kv/": {"type": "kv", "options": {"version": "2"}}})
    state.auth_methods.update(auth_methods or {f"{MOUNT}/": {"type": "jwt"}})
    state.policies.update(policies or {})
    for path, data in (roles or {}).items():
        # A bare name hangs off the default JWT mount; a "<mount>/<name>" key
        # puts the role on the mount it names, which is how the OIDC mount is
        # reached.
        mount, _, name = path.rpartition("/")
        state.paths[f"auth/{mount or MOUNT}/role/{name}"] = dict(data)
    return bao


def clean_namespace(**overrides):
    """A namespace exactly as Ladon's app onboarding leaves it."""
    who = identity()
    role = eso_app_role(who)
    state = {
        "policies": {"default": STOCK_DEFAULT, who.policy_name: eso_app_policy(who).hcl},
        "roles": {role.name: dict(role.data)},
    }
    state.update(overrides)
    return build(**state)


def codes(report):
    return [f.code for f in report.findings]


def find(report, code):
    matches = [f for f in report.findings if f.code == code]
    assert matches, f"expected a {code} finding, got {codes(report)}"
    return matches[0]


# --- the parser ---------------------------------------------------------------


def test_parses_the_policy_ladon_renders():
    grants = parse_policy(eso_app_policy(identity()).hcl)
    assert [g.path for g in grants] == [
        "kv/data/apps/sample/*",
        "kv/data/apps/sample",
        "kv/metadata/apps/sample/*",
        "auth/token/lookup-self",
        "auth/token/renew-self",
        "auth/token/revoke-self",
        "sys/capabilities-self",
    ]
    assert grants[0].capabilities == ("read",)
    assert grants[2].capabilities == ("read", "list")


def test_parser_tolerates_comments_blank_lines_and_one_line_blocks():
    grants = parse_policy(
        '\n\n# a hash comment\n// a slash comment\npath "kv/data/x" { capabilities = ["read"] }\n\n'
        '  path "kv/metadata/x"{capabilities=["read","list",]}\n'
    )
    assert [(g.path, g.capabilities) for g in grants] == [
        ("kv/data/x", ("read",)),
        ("kv/metadata/x", ("read", "list")),
    ]


def test_parser_reads_an_empty_body_as_granting_nothing():
    assert parse_policy("\n  # nothing here\n") == ()


@pytest.mark.parametrize(
    "body",
    [
        'path "kv/data/x" { capabilities = ["read"] } trailing',
        'path "kv/data/x" { allowed_parameters = { a = [] } }',
        'path "" { capabilities = ["read"] }',
        'path "kv/data/x" { capabilities = [read] }',
        "{ not hcl at all }",
    ],
)
def test_parser_refuses_what_it_does_not_understand(body):
    with pytest.raises(PolicyParseError):
        parse_policy(body)


def test_parse_error_names_a_position_and_never_the_body():
    body = 'path "kv/data/secret-looking-path" { allowed_parameters = {} }'
    with pytest.raises(PolicyParseError) as exc:
        parse_policy(body)
    assert "character" in str(exc.value)
    assert "secret-looking-path" not in str(exc.value)


# --- an unreadable policy is only as serious as the policy ----------------------


def test_an_unparsable_default_policy_is_high():
    # The default policy is one of the two this audit claims to judge. If it
    # cannot be read, the claim "clean" is not one the audit can make.
    report = audit_namespace(build(policies={"default": "this is not a policy body"}), NAMESPACE)
    finding = find(report, "unparsed-policy")
    assert finding.severity == HIGH
    assert finding.object == "default"
    assert not report.ok


def test_an_unparsable_app_policy_is_high():
    report = audit_namespace(
        build(policies={"eso-sample-readonly": "path kv/data { read }"}), NAMESPACE
    )
    assert find(report, "unparsed-policy").severity == HIGH


def test_an_unparsable_policy_the_audit_does_not_judge_is_medium():
    # A policy outside the two this audit reasons about is a deployment's own to
    # shape, so an unreadable one is worth saying and is not a failure.
    assert audit_module._unparsed_severity("team-readwrite") == MEDIUM
    assert audit_module._unparsed_severity("default") == HIGH
    assert audit_module._unparsed_severity("eso-sample-readonly") == HIGH


# --- a namespace Ladon rendered ------------------------------------------------


def test_a_namespace_ladon_rendered_audits_clean():
    report = audit_namespace(clean_namespace(), NAMESPACE)
    assert report.findings == ()
    assert report.ok
    assert report.namespace == NAMESPACE


def test_the_audit_reads_inside_the_namespace_it_was_asked_about():
    bao = clean_namespace()
    audit_namespace(bao, NAMESPACE)
    assert bao.reads, "the audit must read something"
    assert {read[-1] for read in bao.reads} == {NAMESPACE}


def test_the_audit_writes_nothing():
    bao = clean_namespace()
    audit_namespace(bao, NAMESPACE)
    assert bao.calls == []


# --- roles ---------------------------------------------------------------------


def role_data(**overrides):
    data = dict(eso_app_role(identity()).data)
    data.update(overrides)
    return data


def audit_role(**overrides):
    who = identity()
    name = eso_app_role(who).name
    return (
        audit_namespace(
            build(
                policies={"default": STOCK_DEFAULT, who.policy_name: eso_app_policy(who).hcl},
                roles={name: role_data(**overrides)},
            ),
            NAMESPACE,
        ),
        f"{MOUNT}/role/{name}",
    )


def test_a_role_that_inherits_the_default_policy_is_high():
    report, obj = audit_role(token_no_default_policy=False)
    finding = find(report, "role-inherits-default")
    assert finding.severity == HIGH
    assert finding.object == obj
    assert "token_no_default_policy" in finding.detail
    assert not report.ok


def test_a_role_missing_the_flag_entirely_is_the_same_finding():
    who = identity()
    data = role_data()
    del data["token_no_default_policy"]
    report = audit_namespace(
        build(
            policies={"default": STOCK_DEFAULT, who.policy_name: eso_app_policy(who).hcl},
            roles={eso_app_role(who).name: data},
        ),
        NAMESPACE,
    )
    assert "role-inherits-default" in codes(report)


def test_the_flag_read_back_as_a_string_is_not_a_finding():
    report, _ = audit_role(token_no_default_policy="true")
    assert "role-inherits-default" not in codes(report)


def test_a_role_with_no_bound_subject_is_high():
    report, obj = audit_role(bound_subject="")
    finding = find(report, "unbound-subject")
    assert finding.severity == HIGH
    assert finding.object == obj
    assert "bound_subject" in finding.detail


def test_every_jwt_role_must_pin_a_subject_not_only_the_application_ones():
    # The shared operator role the real team-onboarding plan writes. It is not
    # an "eso-" role, and it is exactly the role a subject-less audit would wave
    # through: one identity that reads every application in the namespace.
    role = jwt_role(
        "external-secrets-root",
        policies=["external-secrets-readonly"],
        bound_audiences=["bao"],
        bound_subject="system:serviceaccount:external-secrets:external-secrets",
    )
    data = dict(role.data)
    del data["bound_subject"]
    report = audit_namespace(
        build(
            policies={"external-secrets-readonly": external_secrets_read("kv").hcl},
            roles={role.name: data},
        ),
        NAMESPACE,
    )
    finding = find(report, "unbound-subject")
    assert finding.severity == HIGH
    assert finding.object == f"{MOUNT}/role/external-secrets-root"


def test_the_real_team_onboarding_role_pins_a_subject_and_audits_clean():
    role = jwt_role(
        "external-secrets-root",
        policies=["external-secrets-readonly"],
        bound_audiences=["bao"],
        bound_subject="system:serviceaccount:external-secrets:external-secrets",
    )
    report = audit_namespace(
        build(
            policies={"external-secrets-readonly": external_secrets_read("kv").hcl},
            roles={role.name: dict(role.data)},
        ),
        NAMESPACE,
    )
    assert report.findings == ()


@pytest.mark.parametrize(
    "subject",
    [
        "*",
        "system:serviceaccount:*:eso-team-a-sample-abc123",
        "system:serviceaccount:default:default",
        "system:serviceaccount:ladon:default",
        "system:serviceaccount:kube-system:eso-team-a-sample-abc123",
    ],
)
def test_a_subject_that_is_not_one_service_account_is_high(subject):
    report, obj = audit_role(bound_subject=subject)
    finding = find(report, "subject-not-pinned")
    assert finding.severity == HIGH
    assert finding.object == obj


def test_an_eso_role_bound_to_somebody_elses_service_account_is_high():
    # The audience alone proves nothing, and a subject that is not this role's
    # own service account means some other workload can take this role's grants.
    report, _ = audit_role(bound_subject="system:serviceaccount:ladon:eso-team-a-other-999999")
    finding = find(report, "subject-not-pinned")
    assert finding.severity == HIGH
    assert "own service account" in finding.detail


def test_an_eso_role_may_live_in_any_service_account_namespace():
    who = identity()
    report, _ = audit_role(bound_subject=f"system:serviceaccount:platform-ladon:{who.name}")
    assert "subject-not-pinned" not in codes(report)


def test_a_role_with_no_bound_audience_is_reported():
    report, obj = audit_role(bound_audiences=[])
    finding = find(report, "unbound-audience")
    assert finding.severity == MEDIUM
    assert finding.object == obj


def test_a_role_carrying_the_default_policy_by_name_is_high():
    report, _ = audit_role(token_policies=["eso-sample-readonly", "default"])
    assert find(report, "default-in-token-policies").severity == HIGH


def test_a_role_carrying_the_root_policy_by_name_is_high():
    report, _ = audit_role(token_policies=["root"])
    finding = find(report, "privileged-policy")
    assert finding.severity == HIGH
    assert "root" in finding.detail


def test_a_role_with_no_policies_is_reported():
    report, _ = audit_role(token_policies=[])
    assert find(report, "no-token-policies").severity == MEDIUM


def test_a_role_naming_a_policy_that_does_not_exist_is_reported():
    report, _ = audit_role(token_policies=["eso-sample-readonly", "eso-gone-readonly"])
    finding = find(report, "missing-policy")
    assert finding.severity == MEDIUM
    assert "eso-gone-readonly" in finding.detail


def test_an_application_role_carrying_a_second_policy_is_high():
    # The whole point of the per-application identity is that it carries one
    # policy. A team write policy beside it hands the application the namespace.
    report, obj = audit_role(token_policies=["eso-sample-readonly", "team-readwrite"])
    finding = find(report, "extra-policy")
    assert finding.severity == HIGH
    assert finding.object == obj
    assert "team-readwrite" in finding.detail


def test_an_application_role_carrying_the_shared_operator_policy_is_high():
    report, _ = audit_role(token_policies=["external-secrets-readonly"])
    finding = find(report, "extra-policy")
    assert finding.severity == HIGH
    assert "external-secrets-readonly" in finding.detail


def test_an_application_role_carrying_two_application_policies_is_high():
    report, _ = audit_role(token_policies=["eso-sample-readonly", "eso-other-readonly"])
    finding = find(report, "extra-policy")
    assert finding.severity == HIGH


def test_a_jwt_mount_with_no_roles_is_not_a_finding():
    report = audit_namespace(build(policies={"default": STOCK_DEFAULT}), NAMESPACE)
    assert report.findings == ()


# --- which mounts and which roles are audited ---------------------------------------


def test_a_jwt_role_hiding_on_the_oidc_mount_is_audited():
    # An OIDC mount is not a place workload roles are exempt: a role whose
    # role_type is jwt is a workload role wherever it was written.
    report = audit_namespace(
        build(
            auth_methods={"oidc/": {"type": "oidc"}},
            policies={"default": STOCK_DEFAULT, "root": "# built in"},
            roles={"oidc/backdoor": {"role_type": "jwt", "token_policies": ["root"]}},
        ),
        NAMESPACE,
    )
    assert find(report, "privileged-policy").object == "oidc/role/backdoor"
    assert "role-inherits-default" in codes(report)
    assert not report.ok


def test_an_oidc_role_keeps_its_documented_exemption():
    # An operator's interactive session legitimately carries the default policy
    # (it is what lets a token renew and revoke itself) and has no service
    # account to pin, so the workload rules do not apply to it.
    role = oidc_role(
        "operators",
        policies=["team-readonly"],
        allowed_redirect_uris=["https://bao.example:8200/ui/vault/auth/oidc/oidc/callback"],
    )
    readonly = 'path "kv/data/shared/config" { capabilities = ["read"] }'
    report = audit_namespace(
        build(
            auth_methods={"oidc/": {"type": "oidc"}},
            policies={"default": STOCK_DEFAULT, "team-readonly": readonly},
            roles={f"oidc/{role.name}": dict(role.data)},
        ),
        NAMESPACE,
    )
    assert report.findings == ()


# --- the default policy ---------------------------------------------------------


def test_the_stock_default_policy_audits_clean():
    report = audit_namespace(build(policies={"default": STOCK_DEFAULT}), NAMESPACE)
    assert report.findings == ()


def test_a_default_policy_widened_to_the_policy_endpoints_is_high():
    report = audit_namespace(build(policies={"default": WIDENED_DEFAULT}), NAMESPACE)
    finding = find(report, "default-grants-system")
    assert finding.severity == HIGH
    assert finding.object == "default"
    assert finding.detail == "grants create, read, update, delete, list, sudo on sys/policies/acl/*"
    assert not report.ok


def test_a_default_policy_granting_sudo_is_high():
    report = audit_namespace(build(policies={"default": WIDENED_DEFAULT}), NAMESPACE)
    finding = find(report, "default-grants-sudo")
    assert finding.severity == HIGH
    assert finding.detail == "grants sudo on sys/policies/acl/*"


def test_a_default_policy_that_writes_the_kv_mount_is_high():
    report = audit_namespace(build(policies={"default": WIDENED_DEFAULT}), NAMESPACE)
    finding = find(report, "default-widens-kv")
    assert finding.severity == HIGH
    assert "create" in finding.detail
    assert "kv/data/*" in finding.detail


@pytest.mark.parametrize("path", ["sys/auth/*", "sys/mounts/kv", "sys/*"])
def test_the_default_policy_may_not_reach_the_mount_endpoints(path):
    body = f'path "{path}" {{ capabilities = ["read", "list"] }}'
    report = audit_namespace(build(policies={"default": body}), NAMESPACE)
    assert "default-grants-system" in codes(report)


@pytest.mark.parametrize("path", ["kv/delete/*", "kv/undelete/*", "kv/destroy/*"])
def test_the_default_policy_may_not_reach_the_kv_lifecycle_paths(path):
    # Destroying a version is not a read, and the lifecycle paths sit beside
    # data and metadata rather than under them.
    body = f'path "{path}" {{ capabilities = ["update"] }}'
    report = audit_namespace(build(policies={"default": body}), NAMESPACE)
    finding = find(report, "default-widens-kv")
    assert finding.severity == HIGH
    assert path in finding.detail


@pytest.mark.parametrize("path", ["kv/*", "kv/data/*", "kv/metadata/*"])
def test_a_mount_wide_glob_in_the_default_policy_is_high_even_at_read(path):
    # Read on every path in the mount is every secret in the namespace. It is
    # not a widening of capability, so the capability check does not see it.
    body = f'path "{path}" {{ capabilities = ["read", "list"] }}'
    report = audit_namespace(build(policies={"default": body}), NAMESPACE)
    finding = find(report, "default-reads-all-kv")
    assert finding.severity == HIGH
    assert path in finding.detail


def test_a_specific_read_path_in_the_default_policy_stays_clean():
    body = 'path "kv/data/shared/platform-config" { capabilities = ["read"] }'
    report = audit_namespace(build(policies={"default": body}), NAMESPACE)
    assert report.findings == ()


def test_a_prefix_below_the_mount_root_is_not_mount_wide():
    body = 'path "kv/data/shared/*" { capabilities = ["read", "list"] }'
    report = audit_namespace(build(policies={"default": body}), NAMESPACE)
    assert report.findings == ()


def test_an_explicit_deny_in_the_default_policy_is_not_a_finding():
    # A deny block narrows. Reporting it would teach operators that hardening
    # their default policy makes the audit worse.
    body = 'path "sys/policies/acl/*" { capabilities = ["deny"] }'
    report = audit_namespace(build(policies={"default": body}), NAMESPACE)
    assert report.findings == ()


def test_the_kv_mount_is_read_from_the_namespace_not_assumed():
    body = 'path "team-kv/data/x" { capabilities = ["create", "read"] }'
    bao = build(
        policies={"default": body},
        mounts={"team-kv/": {"type": "kv", "options": {"version": "2"}}},
    )
    assert "default-widens-kv" in codes(audit_namespace(bao, NAMESPACE))


def test_a_namespace_with_no_kv_mount_is_reported():
    # No guess: a KV mount assumed into existence audits a mount nothing uses
    # and reports clean about paths it never looked at.
    bao = build(
        policies={"default": STOCK_DEFAULT},
        mounts={"cubbyhole/": {"type": "cubbyhole"}},
    )
    report = audit_namespace(bao, NAMESPACE)
    finding = find(report, "no-kv-mount")
    assert finding.severity == LOW
    assert report.ok


def test_a_namespace_with_no_default_policy_is_not_a_finding():
    body = 'path "kv/data/x" { capabilities = ["read"] }'
    report = audit_namespace(build(policies={"team-readonly": body}), NAMESPACE)
    assert "unparsed-policy" not in codes(report)
    assert report.findings == ()


# --- the per-application policies -------------------------------------------------


def app_policy_report(body, *, app=APP):
    return audit_namespace(build(policies={f"eso-{app}-readonly": body}), NAMESPACE)


def test_the_app_policy_ladon_renders_audits_clean():
    who = identity()
    report = audit_namespace(build(policies={who.policy_name: eso_app_policy(who).hcl}), NAMESPACE)
    assert report.findings == ()


def test_an_app_policy_reaching_another_app_prefix_is_a_cross_app_grant():
    report = app_policy_report('path "kv/data/apps/other/*" { capabilities = ["read"] }')
    finding = find(report, "cross-app-grant")
    assert finding.severity == HIGH
    assert finding.object == "eso-sample-readonly"
    assert "kv/data/apps/other/*" in finding.detail


def test_an_app_policy_granting_the_whole_mount_is_a_cross_app_grant():
    report = app_policy_report('path "kv/data/*" { capabilities = ["read"] }')
    assert "cross-app-grant" in codes(report)


def test_an_app_policy_granting_every_app_is_a_cross_app_grant():
    report = app_policy_report('path "kv/metadata/apps/*" { capabilities = ["read", "list"] }')
    assert "cross-app-grant" in codes(report)


def test_a_glob_on_the_application_name_reaches_its_neighbours():
    # Two applications really live in one namespace, and "apps/sample*" reads
    # both of them. The glob looks like the application's own prefix and is not.
    who = identity()
    neighbour = identity(app="sample-other")
    bao = build(
        policies={
            who.policy_name: 'path "kv/data/apps/sample*" { capabilities = ["read"] }',
            neighbour.policy_name: eso_app_policy(neighbour).hcl,
        }
    )
    report = audit_namespace(bao, NAMESPACE)
    finding = find(report, "cross-app-grant")
    assert finding.object == "eso-sample-readonly"
    assert "kv/data/apps/sample*" in finding.detail
    # The neighbour's own policy, rendered by Ladon, is untouched by this.
    assert [f.object for f in report.findings] == ["eso-sample-readonly"]


def test_a_neighbouring_app_whose_name_starts_the_same_is_still_cross_app():
    report = app_policy_report('path "kv/data/apps/sample-other/*" { capabilities = ["read"] }')
    assert "cross-app-grant" in codes(report)


def test_an_app_policy_granting_anything_outside_the_mount_is_out_of_scope():
    report = app_policy_report('path "sys/mounts/*" { capabilities = ["read"] }')
    finding = find(report, "out-of-scope-grant")
    assert finding.severity == HIGH
    assert "sys/mounts/*" in finding.detail


def test_an_app_policy_that_writes_its_own_prefix_is_overbroad():
    report = app_policy_report('path "kv/data/apps/sample/*" { capabilities = ["read", "update"] }')
    finding = find(report, "overbroad-capability")
    assert finding.severity == HIGH
    assert "update" in finding.detail


def test_the_token_self_paths_at_their_documented_capabilities_are_clean():
    body = "\n".join(
        [
            'path "auth/token/lookup-self" { capabilities = ["read"] }',
            'path "auth/token/renew-self" { capabilities = ["update"] }',
            'path "auth/token/revoke-self" { capabilities = ["update"] }',
            'path "sys/capabilities-self" { capabilities = ["update"] }',
        ]
    )
    assert app_policy_report(body).findings == ()


def test_a_token_self_path_carrying_more_than_it_needs_is_reported():
    # The self paths are an exemption from the prefix rule, not from the
    # capability rule: "sudo" on sys/capabilities-self is not a token looking
    # itself up.
    body = 'path "sys/capabilities-self" { capabilities = ["update", "sudo"] }'
    finding = find(app_policy_report(body), "overbroad-capability")
    assert finding.severity == HIGH
    assert "sudo" in finding.detail


def test_a_policy_that_is_not_an_app_policy_is_left_alone():
    bao = build(policies={"team-readwrite": 'path "kv/data/*" { capabilities = ["create"] }'})
    assert audit_namespace(bao, NAMESPACE).findings == ()


# --- what the namespace holds -------------------------------------------------------


def test_a_namespace_with_nothing_in_it_is_reported():
    bao = FakeOpenBao()
    bao.state(NAMESPACE)
    report = audit_namespace(bao, NAMESPACE)
    finding = find(report, "empty-namespace")
    assert finding.severity == MEDIUM
    assert finding.object == NAMESPACE
    # Nothing here grants anything, so it is not a failure; it is a report that
    # says the audit found nothing to judge rather than judging nothing clean.
    assert report.ok


def test_a_namespace_with_mounts_but_no_policies_is_reported():
    report = audit_namespace(build(policies={}), NAMESPACE)
    assert "empty-namespace" in codes(report)


def test_a_report_carries_what_it_looked_at():
    # One JWT mount, the application's role on it, and two policies: the
    # namespace default and the application's own. A report that says "clean"
    # about nothing is the failure this guards against.
    report = audit_namespace(clean_namespace(), NAMESPACE)
    assert report.counts == {"mounts": 1, "roles": 1, "policies": 2}
    assert report.as_dict()["counts"] == {"mounts": 1, "roles": 1, "policies": 2}


# --- reports ---------------------------------------------------------------------


def test_findings_are_ordered_with_the_high_ones_first():
    report, _ = audit_role(token_no_default_policy=False, bound_audiences=[])
    severities = [f.severity for f in report.findings]
    assert severities == sorted(severities, key=[HIGH, MEDIUM, LOW].index)


def test_a_report_renders_one_line_per_finding():
    report, obj = audit_role(token_no_default_policy=False)
    lines = report.render().splitlines()
    assert len(lines) == len(report.findings)
    assert lines[0].startswith(f"[{NAMESPACE}] {HIGH} role-inherits-default {obj}: ")


def test_a_report_serialises_to_plain_data():
    report, _ = audit_role(token_no_default_policy=False)
    payload = report.as_dict()
    assert payload["namespace"] == NAMESPACE
    assert payload["ok"] is False
    assert set(payload) == {"namespace", "ok", "counts", "findings"}
    assert set(payload["findings"][0]) == {"severity", "code", "object", "detail"}


# --- across namespaces -------------------------------------------------------------


def test_audit_all_covers_the_root_and_every_named_namespace():
    bao = FakeOpenBao()
    bao.state("").policies.update({"default": STOCK_DEFAULT})
    bao.state("ns-a").policies.update({"default": WIDENED_DEFAULT})
    bao.state("ns-b").policies.update({"default": STOCK_DEFAULT})
    reports = audit_all(bao, ["ns-a", "ns-b"])
    assert [r.namespace for r in reports] == ["root", "ns-a", "ns-b"]
    assert [r.ok for r in reports] == [True, False, True]


def test_audit_all_with_no_namespaces_is_the_root_alone():
    reports = audit_all(FakeOpenBao())
    assert [r.namespace for r in reports] == ["root"]


def test_a_client_that_cannot_be_scoped_is_an_error():
    class Unscopeable:
        pass

    with pytest.raises(AuditError):
        audit_namespace(Unscopeable(), NAMESPACE)


def test_a_read_that_fails_is_an_error_naming_the_namespace():
    class Broken(FakeOpenBao):
        def list(self, path):
            raise RuntimeError("connection reset")

    bao = Broken()
    bao.state(NAMESPACE).auth_methods.update({f"{MOUNT}/": {"type": "jwt"}})
    with pytest.raises(AuditError) as exc:
        audit_namespace(bao, NAMESPACE)
    assert NAMESPACE in str(exc.value)


# --- what Ladon itself writes ---------------------------------------------------------


def test_the_real_plans_audit_with_no_findings_at_all():
    """Everything Ladon writes, applied and then audited.

    This is the check that keeps the rest of the file honest. Every rule above
    is a rule about drift, and a rule that fires on Ladon's own output is not a
    rule about drift: it is a bug, in the audit or in what Ladon renders. Root
    config, one team, and three applications in that team's namespace, applied
    through the real executor to the fake, must come back with nothing to say.
    """
    team = Team(name=TEAM, namespace=NAMESPACE, environments=("dev",))
    bao = FakeOpenBao()

    root = root_config_plan(
        policies=[*baseline_team_policies("kv"), external_secrets_read("kv")],
        auth_methods=[
            jwt_auth(discovery_url=ISSUER),
            oidc_auth(
                discovery_url=ISSUER,
                client_id="ladon",
                client_secret_ref="secret/oidc",
            ),
        ],
        kv_mount="kv",
        teams=[team],
    )
    apply_plan(root, client=bao, apply=True)

    apply_plan(
        team_onboarding_plan(
            cluster=CLUSTER,
            oidc_discovery_url=ISSUER,
            kv_mount="kv",
            namespace=NAMESPACE,
        ),
        client=bao,
        apply=True,
    )

    for app in ("sample", "sample-other", "reports"):
        apply_plan(
            app_onboarding_plan(identity(app=app)),
            client=bao,
            apply=True,
        )

    reports = audit_all(bao, [NAMESPACE])
    findings = [f"[{r.namespace}] {f.render()}" for r in reports for f in r.findings]
    assert findings == []
    assert all(r.ok for r in reports)
    # And it really looked. In the team's namespace: three application policies
    # beside the three the baseline writes there (team-readwrite, the ESO read
    # policy, and the one team onboarding names), and four roles on its one
    # JWT mount (the shared operator role plus one per application).
    assert reports[1].counts == {"mounts": 1, "roles": 4, "policies": 6}
    # And in the root: the two auth mounts root config enables.
    assert reports[0].counts["mounts"] == 2
