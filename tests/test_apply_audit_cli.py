"""Tests for the ``audit`` subcommand (Task 8).

The audit connects read-only, so the connection is pointed at the recording
fake and nothing touches a live OpenBao. What is asserted here is the contract
an operator and a later endpoint both depend on: one line per finding, an exit
code that distinguishes clean from failing, and a JSON shape that carries the
same findings.
"""

import io
import json

import pytest

from fake_openbao import FakeOpenBao
from ladon.apply import cli
from ladon.apply.audit import AUDIT_FINDINGS_EXIT

MOUNT = "jwt-cluster-a"

STOCK_DEFAULT = 'path "auth/token/lookup-self" { capabilities = ["read"] }'

WIDENED_DEFAULT = """
path "sys/policies/acl/*" {
  capabilities = ["create", "read", "update", "delete", "list", "sudo"]
}
"""

CONFIG_YAML = """
deployment:
  kv_mount: kv
teams:
  - name: team-a
    namespace: ns-a
    environments: [dev]
  - name: team-b
    namespace: ns-b
    environments: [dev]
"""


def run(argv):
    out = io.StringIO()
    code = cli.main(argv, out=out)
    return code, out.getvalue()


@pytest.fixture(autouse=True)
def bao_env(monkeypatch):
    monkeypatch.setenv("BAO_ADDR", "https://bao.example:8200")
    monkeypatch.setenv("BAO_TOKEN", "s.realtoken")


def point_at(monkeypatch, bao):
    monkeypatch.setattr(cli, "connect", lambda conn, credential, **k: bao)
    return bao


def seed(bao, namespace, *, policies=None, roles=None):
    state = bao.state(namespace)
    state.mounts.update({"kv/": {"type": "kv", "options": {"version": "2"}}})
    state.auth_methods.update({f"{MOUNT}/": {"type": "jwt"}})
    state.policies.update(policies or {})
    for name, data in (roles or {}).items():
        state.paths[f"auth/{MOUNT}/role/{name}"] = dict(data)
    return bao


def write_config(tmp_path):
    path = tmp_path / "deployment.yaml"
    path.write_text(CONFIG_YAML)
    return str(path)


def test_a_clean_audit_exits_zero_and_says_so(monkeypatch):
    point_at(monkeypatch, seed(FakeOpenBao(), "", policies={"default": STOCK_DEFAULT}))
    code, text = run(["audit"])
    assert code == 0
    assert "No findings" in text


def test_a_widened_default_policy_exits_three_and_prints_one_line(monkeypatch):
    point_at(monkeypatch, seed(FakeOpenBao(), "ns-a", policies={"default": WIDENED_DEFAULT}))
    code, text = run(["audit", "--namespace", "ns-a"])
    assert code == AUDIT_FINDINGS_EXIT
    assert (
        "[ns-a] high default-grants-system default: grants create, read, update, "
        "delete, list, sudo on sys/policies/acl/*"
    ) in text


def test_a_role_that_inherits_the_default_policy_prints_one_line(monkeypatch):
    role = {
        "role_type": "jwt",
        "token_policies": ["eso-sample-readonly"],
        "bound_audiences": ["openbao"],
        "bound_subject": "system:serviceaccount:ladon:eso-team-a-sample-abc123",
    }
    bao = seed(
        FakeOpenBao(),
        "ns-a",
        policies={
            "default": STOCK_DEFAULT,
            "eso-sample-readonly": 'path "kv/data/apps/sample/*" { capabilities = ["read"] }',
        },
        roles={"eso-team-a-sample-abc123": role},
    )
    point_at(monkeypatch, bao)
    code, text = run(["audit", "--namespace", "ns-a"])
    assert code == AUDIT_FINDINGS_EXIT
    assert (
        "[ns-a] high role-inherits-default jwt-cluster-a/role/eso-team-a-sample-abc123: "
        "token_no_default_policy is not set, so this role also carries the namespace "
        "default policy"
    ) in text


def test_the_namespaces_come_from_the_config(monkeypatch, tmp_path):
    bao = FakeOpenBao()
    seed(bao, "", policies={"default": STOCK_DEFAULT})
    seed(bao, "ns-a", policies={"default": STOCK_DEFAULT})
    seed(bao, "ns-b", policies={"default": WIDENED_DEFAULT})
    point_at(monkeypatch, bao)
    code, text = run(["audit", "--config", write_config(tmp_path)])
    assert code == AUDIT_FINDINGS_EXIT
    assert "[ns-b] high default-grants-system" in text
    assert "[ns-a]" not in text


def test_an_explicit_namespace_audits_only_that_one(monkeypatch, tmp_path):
    bao = FakeOpenBao()
    seed(bao, "ns-a", policies={"default": WIDENED_DEFAULT})
    seed(bao, "ns-b", policies={"default": WIDENED_DEFAULT})
    point_at(monkeypatch, bao)
    code, text = run(["audit", "--namespace", "ns-a", "--config", write_config(tmp_path)])
    assert code == AUDIT_FINDINGS_EXIT
    assert "[ns-b]" not in text


def test_json_output_carries_the_same_findings(monkeypatch):
    point_at(monkeypatch, seed(FakeOpenBao(), "ns-a", policies={"default": WIDENED_DEFAULT}))
    code, text = run(["audit", "--namespace", "ns-a", "--json"])
    assert code == AUDIT_FINDINGS_EXIT
    payload = json.loads(text)
    assert payload["ok"] is False
    assert payload["namespaces"][0]["namespace"] == "ns-a"
    codes = [f["code"] for f in payload["namespaces"][0]["findings"]]
    assert "default-grants-system" in codes


def test_json_output_of_a_clean_audit(monkeypatch):
    point_at(monkeypatch, seed(FakeOpenBao(), "", policies={"default": STOCK_DEFAULT}))
    code, text = run(["audit", "--json"])
    assert code == 0
    payload = json.loads(text)
    assert payload["ok"] is True
    assert payload["namespaces"] == [{"namespace": "root", "ok": True, "findings": []}]


def test_a_connection_failure_is_reported_without_a_traceback(monkeypatch):
    def boom(*a, **k):
        raise cli.BaoConnectionError("could not reach OpenBao")

    monkeypatch.setattr(cli, "connect", boom)
    code, _ = run(["audit"])
    assert code == 2


def test_the_audit_writes_nothing(monkeypatch):
    bao = point_at(monkeypatch, seed(FakeOpenBao(), "", policies={"default": WIDENED_DEFAULT}))
    run(["audit"])
    assert bao.calls == []
