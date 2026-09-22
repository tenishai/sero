"""Tests for the pure ESO resource rendering (D4/D5)."""

import pytest
import yaml

from ladon.config.eso import (
    API_GROUP,
    awssm_store,
    external_secret,
    gcpsm_store,
    to_manifest,
    vault_store,
)
from ladon.config.teams import ConfigError


def test_vault_store_shape_and_auth():
    store = vault_store(
        name="bao",
        server="https://bao.example:8200",
        auth_mount="jwt-team-a",
        role="external-secrets-root",
        audiences=["bao"],
        namespace="team-a",
    )
    assert store.provider == "vault"
    spec = store.manifest["spec"]["provider"]["vault"]
    assert spec["server"] == "https://bao.example:8200"
    assert spec["path"] == "kv"
    assert spec["version"] == "v2"
    jwt = spec["auth"]["jwt"]
    assert jwt["path"] == "jwt-team-a"
    assert jwt["role"] == "external-secrets-root"
    sat = jwt["kubernetesServiceAccountToken"]
    assert sat["serviceAccountRef"] == {"name": "external-secrets", "namespace": "external-secrets"}
    assert sat["audiences"] == ["bao"]


def test_vault_store_reads_the_teams_openbao_namespace():
    # Without this the store can only read the root namespace's KV, which is not
    # where a team's secrets live.
    store = vault_store(
        name="bao",
        server="s",
        auth_mount="j",
        role="r",
        audiences=["a"],
        namespace="team-a",
    )
    assert store.manifest["spec"]["provider"]["vault"]["namespace"] == "team-a"


def test_vault_store_is_restricted_to_the_team_kubernetes_namespace():
    # A ClusterSecretStore is cluster-wide by default, so any namespace could
    # reference it and read the team's secrets. The API server sets
    # kubernetes.io/metadata.name and it cannot be forged by relabeling, unlike
    # a team-managed label.
    store = vault_store(
        name="bao",
        server="s",
        auth_mount="j",
        role="r",
        audiences=["a"],
        namespace="team-a",
    )
    assert store.manifest["spec"]["conditions"] == [
        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "team-a"}}}
    ]


def test_vault_store_accepts_several_kubernetes_namespaces():
    store = vault_store(
        name="bao",
        server="s",
        auth_mount="j",
        role="r",
        audiences=["a"],
        namespace="team-a",
        k8s_namespaces=["team-a-dev", "team-a-qa"],
    )
    assert store.manifest["spec"]["conditions"] == [
        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "team-a-dev"}}},
        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "team-a-qa"}}},
    ]


def test_vault_store_requires_server_role_audiences_and_namespace():
    with pytest.raises(ConfigError, match="'server'"):
        vault_store(name="x", server="", auth_mount="j", role="r", audiences=["a"], namespace="n")
    with pytest.raises(ConfigError, match="'role'"):
        vault_store(name="x", server="s", auth_mount="j", role="", audiences=["a"], namespace="n")
    with pytest.raises(ConfigError, match="'audiences'"):
        vault_store(name="x", server="s", auth_mount="j", role="r", audiences=[], namespace="n")
    with pytest.raises(ConfigError, match="'namespace'"):
        vault_store(name="x", server="s", auth_mount="j", role="r", audiences=["a"], namespace="")
    with pytest.raises(ConfigError, match="'k8s_namespaces'"):
        vault_store(
            name="x",
            server="s",
            auth_mount="j",
            role="r",
            audiences=["a"],
            namespace="n",
            k8s_namespaces=[],
        )


def test_gcpsm_store_workload_identity():
    store = gcpsm_store(
        name="gcpsm",
        project_id="proj",
        cluster_name="bao-east",
        cluster_location="us-east4",
        cluster_project_id="proj",
    )
    assert store.provider == "gcpsm"
    gcpsm = store.manifest["spec"]["provider"]["gcpsm"]
    assert gcpsm["projectID"] == "proj"
    wi = gcpsm["auth"]["workloadIdentity"]
    assert wi["clusterName"] == "bao-east"
    assert wi["clusterLocation"] == "us-east4"
    assert wi["serviceAccountRef"]["name"] == "external-secrets"


def test_gcpsm_store_requires_cluster_fields():
    with pytest.raises(ConfigError, match="cluster_name"):
        gcpsm_store(
            name="g",
            project_id="p",
            cluster_name="",
            cluster_location="us-east4",
            cluster_project_id="p",
        )


def test_awssm_store_irsa_without_role():
    store = awssm_store(name="awssm", region="us-east-1")
    assert store.provider == "aws"
    aws = store.manifest["spec"]["provider"]["aws"]
    assert aws["service"] == "SecretsManager"
    assert aws["region"] == "us-east-1"
    assert "role" not in aws
    assert aws["auth"]["jwt"]["serviceAccountRef"]["name"] == "external-secrets"


def test_awssm_store_with_cross_account_role():
    store = awssm_store(
        name="awssm",
        region="us-west-2",
        role="arn:aws:iam::123456789012:role/eso",
    )
    assert store.manifest["spec"]["provider"]["aws"]["role"] == "arn:aws:iam::123456789012:role/eso"


def test_awssm_store_requires_region():
    with pytest.raises(ConfigError, match="'region'"):
        awssm_store(name="a", region="")


def test_external_secret_shape():
    es = external_secret(
        name="app-secrets",
        namespace="team-a",
        store_name="bao-team-a",
        kv_path="team-a/app",
        secret_name="app-secrets",
    )
    assert es["kind"] == "ExternalSecret"
    assert es["metadata"] == {"name": "app-secrets", "namespace": "team-a"}
    spec = es["spec"]
    assert spec["secretStoreRef"] == {"name": "bao-team-a", "kind": "ClusterSecretStore"}
    assert spec["target"] == {"name": "app-secrets", "creationPolicy": "Owner"}
    assert spec["dataFrom"] == [{"extract": {"key": "team-a/app"}}]
    assert spec["refreshInterval"] == "1h"


def test_external_secret_keeps_an_existing_secret_name_and_refresh_interval():
    # A consumer adopting ESO keeps the Secret name its workloads already mount.
    es = external_secret(
        name="app",
        namespace="team-a",
        store_name="bao-team-a",
        kv_path="team-a/app",
        secret_name="legacy-app-secrets",
        refresh_interval="15m",
    )
    assert es["spec"]["target"]["name"] == "legacy-app-secrets"
    assert es["spec"]["refreshInterval"] == "15m"


def test_external_secret_validates_required():
    with pytest.raises(ConfigError, match="'kv_path'"):
        external_secret(
            name="n",
            namespace="ns",
            store_name="s",
            kv_path="",
            secret_name="sec",
        )


def test_api_version_defaults_to_v1_and_overrides():
    assert (
        vault_store(
            name="x", server="s", auth_mount="j", role="r", audiences=["a"], namespace="n"
        ).manifest["apiVersion"]
        == f"{API_GROUP}/v1"
    )
    legacy = awssm_store(name="x", region="r", api_version="v1beta1")
    assert legacy.manifest["apiVersion"] == f"{API_GROUP}/v1beta1"
    es = external_secret(
        name="n",
        namespace="ns",
        store_name="s",
        kv_path="p",
        secret_name="sec",
        api_version="v1beta1",
    )
    assert es["apiVersion"] == f"{API_GROUP}/v1beta1"


def test_to_manifest_round_trips_yaml():
    store = vault_store(
        name="bao", server="s", auth_mount="j", role="r", audiences=["bao"], namespace="team-a"
    )
    text = to_manifest(store)
    loaded = yaml.safe_load(text)
    assert loaded == store.manifest
    # Accepts a plain dict (ExternalSecret) too.
    es = external_secret(name="n", namespace="ns", store_name="s", kv_path="p", secret_name="sec")
    assert yaml.safe_load(to_manifest(es)) == es


def test_no_secret_value_in_rendered_output():
    # The store and ExternalSecret carry references and paths, never values.
    store = vault_store(
        name="bao", server="s", auth_mount="j", role="r", audiences=["bao"], namespace="team-a"
    )
    es = external_secret(
        name="n", namespace="ns", store_name="bao", kv_path="team/app", secret_name="app"
    )
    blob = to_manifest(store) + to_manifest(es)
    # creationPolicy Owner means ESO writes the Secret at runtime; the manifest
    # never embeds secret material. No inline `data:`/`stringData:` block exists
    # (checked as a key, so `metadata:` does not count), and ESO pulls by path.
    keys = {line.strip().rstrip(":") for line in blob.splitlines() if line.strip().endswith(":")}
    assert "data" not in keys
    assert "stringData" not in keys
