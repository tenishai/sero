"""Tests for the role and policy audit (Task 8).

Everything here runs against the recording fake, so the audit is exercised
against real response shapes without a live OpenBao. The fixtures build two
kinds of namespace: one Ladon itself rendered (which must audit clean) and one
that has been widened by hand in the ways the sibling platform was, which must
be reported.

No fixture carries a secret value: a policy body is a set of paths and
capabilities, and a role is a set of bindings.
"""

import pytest

from fake_openbao import FakeOpenBao
from ladon.apply.audit import (
    HIGH,
    MEDIUM,
    AuditError,
    PolicyParseError,
    audit_all,
    audit_namespace,
    parse_policy,
)
from ladon.config.apps import app_identity, eso_app_policy, eso_app_role

TEAM = "team-a"
APP = "sample"
CLUSTER = "cluster-a"
NAMESPACE = "ns-a"
MOUNT = f"jwt-{CLUSTER}"

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


def identity(app=APP):
    return app_identity(team=TEAM, app=app, cluster=CLUSTER, namespace=NAMESPACE)


def build(*, policies=None, roles=None, mounts=None, auth_methods=None, namespace=NAMESPACE):
    """A fake OpenBao holding one namespace's live state."""
    bao = FakeOpenBao()
    state = bao.state(namespace)
    state.mounts.update(mounts or {"kv/": {"type": "kv", "options": {"version": "2"}}})
    state.auth_methods.update(auth_methods or {f"{MOUNT}/": {"type": "jwt"}})
    state.policies.update(policies or {})
    for name, data in (roles or {}).items():
        state.paths[f"auth/{MOUNT}/role/{name}"] = dict(data)
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


def test_an_unparsable_policy_is_a_finding_not_a_crash():
    report = audit_namespace(build(policies={"default": "this is not a policy body"}), NAMESPACE)
    finding = find(report, "unparsed-policy")
    assert finding.severity == MEDIUM
    assert finding.object == "default"
    # A medium finding, so the namespace is not reported as failing on it alone.
    assert report.ok


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


def test_an_eso_role_with_no_bound_subject_is_high():
    report, obj = audit_role(bound_subject="")
    finding = find(report, "unbound-subject")
    assert finding.severity == HIGH
    assert finding.object == obj
    assert "bound_subject" in finding.detail


def test_a_role_that_is_not_an_eso_role_is_not_asked_for_a_subject():
    data = role_data()
    data.pop("bound_subject")
    report = audit_namespace(
        build(policies={"default": STOCK_DEFAULT}, roles={"operators": data}),
        NAMESPACE,
    )
    assert "unbound-subject" not in codes(report)


def test_a_role_with_no_bound_audience_is_reported():
    report, obj = audit_role(bound_audiences=[])
    finding = find(report, "unbound-audience")
    assert finding.severity == MEDIUM
    assert finding.object == obj


def test_a_role_carrying_the_default_policy_by_name_is_high():
    report, _ = audit_role(token_policies=["eso-sample-readonly", "default"])
    assert find(report, "default-in-token-policies").severity == HIGH


def test_a_role_with_no_policies_is_reported():
    report, _ = audit_role(token_policies=[])
    assert find(report, "no-token-policies").severity == MEDIUM


def test_a_role_naming_a_policy_that_does_not_exist_is_reported():
    report, _ = audit_role(token_policies=["eso-sample-readonly", "eso-gone-readonly"])
    finding = find(report, "missing-policy")
    assert finding.severity == MEDIUM
    assert "eso-gone-readonly" in finding.detail


def test_a_jwt_mount_with_no_roles_is_not_a_finding():
    report = audit_namespace(build(policies={"default": STOCK_DEFAULT}), NAMESPACE)
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
    assert finding.detail == (
        "grants create, read, update, delete, list, sudo on sys/policies/acl/*"
    )
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


def test_the_default_policy_may_read_the_kv_mount():
    body = 'path "kv/data/*" { capabilities = ["read", "list"] }'
    report = audit_namespace(build(policies={"default": body}), NAMESPACE)
    assert report.findings == ()


def test_the_kv_mount_is_read_from_the_namespace_not_assumed():
    body = 'path "team-kv/data/*" { capabilities = ["create", "read"] }'
    bao = build(
        policies={"default": body},
        mounts={"team-kv/": {"type": "kv", "options": {"version": "2"}}},
    )
    assert "default-widens-kv" in codes(audit_namespace(bao, NAMESPACE))


def test_a_namespace_with_no_default_policy_is_not_a_finding():
    report = audit_namespace(build(policies={}), NAMESPACE)
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


def test_the_token_self_paths_are_not_findings():
    body = "\n".join(
        [
            'path "auth/token/lookup-self" { capabilities = ["read"] }',
            'path "auth/token/renew-self" { capabilities = ["update"] }',
            'path "auth/token/revoke-self" { capabilities = ["update"] }',
            'path "sys/capabilities-self" { capabilities = ["update"] }',
        ]
    )
    assert app_policy_report(body).findings == ()


def test_a_policy_that_is_not_an_app_policy_is_left_alone():
    bao = build(policies={"team-readwrite": 'path "kv/data/*" { capabilities = ["create"] }'})
    assert audit_namespace(bao, NAMESPACE).findings == ()


# --- reports ---------------------------------------------------------------------


def test_findings_are_ordered_with_the_high_ones_first():
    report, _ = audit_role(token_no_default_policy=False, bound_audiences=[])
    severities = [f.severity for f in report.findings]
    assert severities == sorted(severities, key=[HIGH, MEDIUM].index)


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
