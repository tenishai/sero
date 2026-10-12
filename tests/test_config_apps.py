"""Tests for per-application ESO identities (D17, ``sero.config.apps``).

One app gets one path prefix, one policy scoped to it, one JWT role pinned to a
dedicated service account, and one store restricted to one Kubernetes namespace.
The policy body is asserted character for character: it is the grant, and a
widened path here is the whole isolation model gone. The identity names are
asserted literally for the same reason: they are what the cluster-wide objects
are keyed by, so two different applications must never render the same one.
"""

import pytest

from sero.config.apps import (
    DEFAULT_APP_AUDIENCE,
    DEFAULT_SA_NAMESPACE,
    app_cluster_documents,
    app_identity,
    eso_app_policy,
    eso_app_role,
    eso_app_service_account,
    eso_app_store,
)
from sero.config.eso import K8S_NAMESPACE_NAME_LABEL
from sero.config.teams import ConfigError

SERVER = "https://bao.example:8200"

# The first six hex of sha256("team-a\x00sample"), which is what disambiguates
# the identity names below. Written out rather than recomputed, so a change to
# how it is derived shows up here as the rename it would really be.
SAMPLE_DIGEST = "a84085"
SAMPLE_NAME = f"eso-team-a-sample-{SAMPLE_DIGEST}"

# Exactly what an app's ESO identity may do: its own prefix, and the token-self
# paths every token needs once the "default" policy is dropped from the role.
EXPECTED_POLICY = """path "kv/data/apps/sample/*" {
  capabilities = ["read"]
}

path "kv/data/apps/sample" {
  capabilities = ["read"]
}

path "kv/metadata/apps/sample/*" {
  capabilities = ["read", "list"]
}

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
"""


def identity(**overrides):
    params = {"team": "team-a", "app": "sample", "cluster": "cluster-a"}
    params.update(overrides)
    return app_identity(**params)


# Names -----------------------------------------------------------------------


def test_names_are_team_qualified_where_they_are_cluster_wide():
    ident = identity()
    # The policy lives inside the team namespace, so the app name is enough; the
    # role, service account and store are reachable from outside it, so each is
    # qualified by the team to stop one team's identity naming another's.
    assert ident.policy_name == "eso-sample-readonly"
    assert ident.name == SAMPLE_NAME
    assert ident.store_name == f"sero-team-a-sample-{SAMPLE_DIGEST}"
    assert ident.auth_mount == "jwt-cluster-a"


def test_two_different_applications_never_share_a_name():
    # Both parts may contain hyphens, so "<team>-<app>" alone is ambiguous:
    # team "a-b" app "c" and team "a" app "b-c" both read as "a-b-c". They live
    # in different OpenBao namespaces but share one cluster, so an ambiguous
    # name would give them the same service account, store and bound subject.
    first = app_identity(team="a-b", app="c", cluster="cluster-a")
    second = app_identity(team="a", app="b-c", cluster="cluster-a")

    assert first.name != second.name
    assert first.store_name != second.store_name
    assert first.bound_subject != second.bound_subject


def test_the_name_is_stable_for_the_same_application():
    # It is an identity, not a version: onboarding twice must not rename it.
    assert identity().name == identity().name
    assert identity(k8s_namespace="elsewhere", audience="other").name == identity().name


def test_a_different_application_in_the_same_team_gets_a_different_name():
    assert identity(app="other").name != identity().name


def test_bound_subject_names_the_service_account_in_seros_own_namespace():
    assert identity().bound_subject == f"system:serviceaccount:sero:{SAMPLE_NAME}"
    assert DEFAULT_SA_NAMESPACE == "sero"


def test_openbao_namespace_defaults_to_the_team_and_is_overridable():
    assert identity().namespace == "team-a"
    assert identity(namespace="ns-a").namespace == "ns-a"


def test_a_nested_openbao_namespace_is_accepted():
    # Namespaces are a path, so a team under a parent namespace is legitimate;
    # every segment is still a label.
    assert identity(namespace="platform/team-a").namespace == "platform/team-a"


def test_kubernetes_namespace_defaults_to_the_app_and_is_overridable():
    assert identity().k8s_namespace == "sample"
    assert identity(k8s_namespace="sero-sample").k8s_namespace == "sero-sample"


# Policy ----------------------------------------------------------------------


def test_policy_grants_exactly_the_app_prefix_and_the_token_self_paths():
    policy = eso_app_policy(identity())
    assert policy.name == "eso-sample-readonly"
    assert policy.hcl == EXPECTED_POLICY


def test_policy_follows_the_deployments_kv_mount():
    policy = eso_app_policy(identity(kv_mount="team-kv"))
    assert 'path "team-kv/data/apps/sample/*"' in policy.hcl
    assert "kv/data" not in policy.hcl.replace("team-kv/data", "")


def test_policy_grants_nothing_on_another_app():
    hcl = eso_app_policy(identity()).hcl
    assert "apps/other" not in hcl
    assert 'path "kv/data/*"' not in hcl


# Role ------------------------------------------------------------------------


def test_role_is_pinned_to_the_service_account_the_policy_and_no_default():
    role = eso_app_role(identity())
    assert role.name == SAMPLE_NAME
    assert role.data["role_type"] == "jwt"
    assert role.data["user_claim"] == "sub"
    assert role.data["bound_audiences"] == [DEFAULT_APP_AUDIENCE]
    assert role.data["bound_subject"] == f"system:serviceaccount:sero:{SAMPLE_NAME}"
    assert role.data["token_policies"] == ["eso-sample-readonly"]
    assert role.data["token_no_default_policy"] is True
    assert role.data["token_ttl"]


def test_default_audience_is_the_one_the_store_requests():
    ident = identity()
    role = eso_app_role(ident)
    store = eso_app_store(ident, server=SERVER)
    requested = store.manifest["spec"]["provider"]["vault"]["auth"]["jwt"][
        "kubernetesServiceAccountToken"
    ]["audiences"]
    assert role.data["bound_audiences"] == requested == [DEFAULT_APP_AUDIENCE]


def test_audience_is_a_parameter_and_both_halves_follow_it():
    ident = identity(audience="sero")
    assert eso_app_role(ident).data["bound_audiences"] == ["sero"]
    token = eso_app_store(ident, server=SERVER).manifest["spec"]["provider"]["vault"]["auth"]["jwt"]
    assert token["kubernetesServiceAccountToken"]["audiences"] == ["sero"]


def test_an_empty_audience_is_refused():
    # An unbound audience is a role any token can present for.
    with pytest.raises(ConfigError, match="audience"):
        identity(audience="")


# Cluster documents -----------------------------------------------------------


def test_service_account_does_not_automount_its_token():
    # The token is projected with an audience by ESO, not mounted into a pod.
    # A default automount is a second, unaudienced token nothing here needs.
    sa = eso_app_service_account(identity())
    assert sa == {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": SAMPLE_NAME, "namespace": "sero"},
        "automountServiceAccountToken": False,
    }


def test_service_account_follows_the_service_account_namespace():
    sa = eso_app_service_account(identity(sa_namespace="sero-system"))
    assert sa["metadata"]["namespace"] == "sero-system"


def test_store_reads_the_team_namespace_through_the_clusters_jwt_mount():
    store = eso_app_store(identity(namespace="ns-a"), server=SERVER)
    vault = store.manifest["spec"]["provider"]["vault"]
    assert store.name == f"sero-team-a-sample-{SAMPLE_DIGEST}"
    assert vault["server"] == SERVER
    assert vault["namespace"] == "ns-a"
    assert vault["path"] == "kv"
    assert vault["auth"]["jwt"]["path"] == "jwt-cluster-a"
    assert vault["auth"]["jwt"]["role"] == SAMPLE_NAME


def test_store_authenticates_as_the_apps_own_service_account():
    store = eso_app_store(identity(), server=SERVER)
    ref = store.manifest["spec"]["provider"]["vault"]["auth"]["jwt"][
        "kubernetesServiceAccountToken"
    ]["serviceAccountRef"]
    assert ref == {"name": SAMPLE_NAME, "namespace": "sero"}


def test_store_admits_only_the_apps_kubernetes_namespace():
    store = eso_app_store(identity(k8s_namespace="sero-sample"), server=SERVER)
    assert store.manifest["spec"]["conditions"] == [
        {"namespaceSelector": {"matchLabels": {K8S_NAMESPACE_NAME_LABEL: "sero-sample"}}}
    ]


def test_documents_are_the_service_account_then_the_store():
    docs = app_cluster_documents(identity(), server=SERVER)
    assert [d["kind"] for d in docs] == ["ServiceAccount", "ClusterSecretStore"]


def test_store_requires_a_server():
    with pytest.raises(ConfigError, match="server"):
        eso_app_store(identity(), server="")


# Validation ------------------------------------------------------------------


@pytest.mark.parametrize("app", ["Sample", "sample_app", "-sample", "sample-", "", "a" * 64])
def test_app_name_must_be_a_dns_label(app):
    with pytest.raises(ConfigError, match="app"):
        identity(app=app)


@pytest.mark.parametrize("team", ["Team-A", "team a", "team.a", ""])
def test_team_name_must_be_a_dns_label(team):
    with pytest.raises(ConfigError, match="team"):
        identity(team=team)


def test_namespaces_must_be_dns_labels():
    with pytest.raises(ConfigError, match="k8s_namespace"):
        identity(k8s_namespace="Sample")
    with pytest.raises(ConfigError, match="sa_namespace"):
        identity(sa_namespace="Sero")
    with pytest.raises(ConfigError, match="namespace"):
        identity(namespace="Team_A")


@pytest.mark.parametrize("namespace", ["platform/Team-A", "platform//team-a", "/team-a"])
def test_every_segment_of_a_nested_namespace_is_validated(namespace):
    with pytest.raises(ConfigError, match="namespace"):
        identity(namespace=namespace)


def test_cluster_is_required():
    with pytest.raises(ConfigError, match="cluster"):
        identity(cluster="")
