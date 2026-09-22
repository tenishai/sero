"""Tests for per-application ESO identities (D17, ``ladon.config.apps``).

One app gets one path prefix, one policy scoped to it, one JWT role pinned to a
dedicated service account, and one store restricted to one Kubernetes namespace.
The policy body is asserted character for character: it is the grant, and a
widened path here is the whole isolation model gone.
"""

import pytest

from ladon.config.apps import (
    DEFAULT_APP_AUDIENCE,
    DEFAULT_SA_NAMESPACE,
    app_cluster_documents,
    app_identity,
    eso_app_policy,
    eso_app_role,
    eso_app_service_account,
    eso_app_store,
)
from ladon.config.eso import K8S_NAMESPACE_NAME_LABEL
from ladon.config.teams import ConfigError

SERVER = "https://bao.example:8200"

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
    assert ident.name == "eso-team-a-sample"
    assert ident.store_name == "ladon-team-a-sample"
    assert ident.auth_mount == "jwt-cluster-a"


def test_bound_subject_names_the_service_account_in_ladons_own_namespace():
    assert identity().bound_subject == "system:serviceaccount:ladon:eso-team-a-sample"
    assert DEFAULT_SA_NAMESPACE == "ladon"


def test_openbao_namespace_defaults_to_the_team_and_is_overridable():
    assert identity().namespace == "team-a"
    assert identity(namespace="ns-a").namespace == "ns-a"


def test_kubernetes_namespace_defaults_to_the_app_and_is_overridable():
    assert identity().k8s_namespace == "sample"
    assert identity(k8s_namespace="ladon-sample").k8s_namespace == "ladon-sample"


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
    assert role.name == "eso-team-a-sample"
    assert role.data["role_type"] == "jwt"
    assert role.data["user_claim"] == "sub"
    assert role.data["bound_audiences"] == [DEFAULT_APP_AUDIENCE]
    assert role.data["bound_subject"] == "system:serviceaccount:ladon:eso-team-a-sample"
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
    ident = identity(audience="ladon")
    assert eso_app_role(ident).data["bound_audiences"] == ["ladon"]
    token = eso_app_store(ident, server=SERVER).manifest["spec"]["provider"]["vault"]["auth"]["jwt"]
    assert token["kubernetesServiceAccountToken"]["audiences"] == ["ladon"]


# Cluster documents -----------------------------------------------------------


def test_service_account_does_not_automount_its_token():
    # The token is projected with an audience by ESO, not mounted into a pod.
    # A default automount is a second, unaudienced token nothing here needs.
    sa = eso_app_service_account(identity())
    assert sa == {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": "eso-team-a-sample", "namespace": "ladon"},
        "automountServiceAccountToken": False,
    }


def test_service_account_follows_the_service_account_namespace():
    sa = eso_app_service_account(identity(sa_namespace="ladon-system"))
    assert sa["metadata"]["namespace"] == "ladon-system"


def test_store_reads_the_team_namespace_through_the_clusters_jwt_mount():
    store = eso_app_store(identity(namespace="ns-a"), server=SERVER)
    vault = store.manifest["spec"]["provider"]["vault"]
    assert store.name == "ladon-team-a-sample"
    assert vault["server"] == SERVER
    assert vault["namespace"] == "ns-a"
    assert vault["path"] == "kv"
    assert vault["auth"]["jwt"]["path"] == "jwt-cluster-a"
    assert vault["auth"]["jwt"]["role"] == "eso-team-a-sample"


def test_store_authenticates_as_the_apps_own_service_account():
    store = eso_app_store(identity(), server=SERVER)
    ref = store.manifest["spec"]["provider"]["vault"]["auth"]["jwt"][
        "kubernetesServiceAccountToken"
    ]["serviceAccountRef"]
    assert ref == {"name": "eso-team-a-sample", "namespace": "ladon"}


def test_store_admits_only_the_apps_kubernetes_namespace():
    store = eso_app_store(identity(k8s_namespace="ladon-sample"), server=SERVER)
    assert store.manifest["spec"]["conditions"] == [
        {"namespaceSelector": {"matchLabels": {K8S_NAMESPACE_NAME_LABEL: "ladon-sample"}}}
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
        identity(sa_namespace="Ladon")
    with pytest.raises(ConfigError, match="namespace"):
        identity(namespace="Team_A")


def test_cluster_is_required():
    with pytest.raises(ConfigError, match="cluster"):
        identity(cluster="")
