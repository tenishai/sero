"""Render External Secrets Operator resources (D4/D5, the secret-sync layer).

ESO is how Ladon syncs secrets out of OpenBao into the supported targets
(Kubernetes, GCP Secret Manager, AWS Secrets Manager). This module renders the
two resources that drive it, from parameters into plain data:

* a ``ClusterSecretStore`` for each provider, describing where ESO reads from or
  writes to and how it authenticates;
* an ``ExternalSecret`` that pulls a secret from a store into a Kubernetes Secret.

Rendering is pure: builders return plain dicts, and ``to_manifest`` serializes
one to YAML. Applying the manifests to a cluster is a separate step.

Multi-cloud and multi-provider by design: the three store providers share one
shape and are selected by builder, matching the brand-neutral, pluggable rule
used elsewhere. The ESO API group version is a parameter (``api_version``)
defaulting to ``v1``; ESO v0.x served ``v1beta1`` but v1.x/v2.x serve ``v1``, so
the default tracks current ESO with an escape hatch for older clusters.

Secret hygiene: nothing here holds a secret value. A store carries auth
*references* (a service account name, a JWT role, a workload-identity binding);
an ExternalSecret carries the KV *path* it reads, never the data. ESO itself
resolves the actual values at runtime, inside the cluster.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import yaml

from ladon.config.validate import require

DEFAULT_API_VERSION = "v1"
API_GROUP = "external-secrets.io"

# The conventional ESO service account and the namespace it runs in. Both are
# overridable; these defaults match the common install.
DEFAULT_ESO_SA = "external-secrets"
DEFAULT_ESO_NAMESPACE = "external-secrets"


@dataclass(frozen=True)
class ClusterSecretStore:
    """A rendered ClusterSecretStore: its name, provider, and the manifest dict."""

    name: str
    provider: str
    manifest: dict = field(default_factory=dict)


def _sa_ref(name: str, namespace: str) -> dict:
    return {"name": name, "namespace": namespace}


def _store(name: str, provider_block: dict, api_version: str) -> ClusterSecretStore:
    provider = next(iter(provider_block))
    manifest = {
        "apiVersion": f"{API_GROUP}/{api_version}",
        "kind": "ClusterSecretStore",
        "metadata": {"name": name},
        "spec": {"provider": provider_block},
    }
    return ClusterSecretStore(name=name, provider=provider, manifest=manifest)


def vault_store(
    *,
    name: str,
    server: str,
    auth_mount: str,
    role: str,
    audiences: list[str],
    mount_path: str = "kv",
    sa_name: str = DEFAULT_ESO_SA,
    sa_namespace: str = DEFAULT_ESO_NAMESPACE,
    api_version: str = DEFAULT_API_VERSION,
) -> ClusterSecretStore:
    """A store that reads from OpenBao over the JWT auth method.

    ESO authenticates with its projected service-account token against the
    per-cluster JWT mount and ``role`` (the external-secrets role from team
    onboarding); ``audiences`` must match the role's bound audiences. No secret
    is stored: the token is minted in-cluster and verified by OpenBao.
    """
    require(server, "server", context="vault_store")
    require(role, "role", context="vault_store")
    require(audiences, "audiences", context="vault_store")
    provider = {
        "vault": {
            "server": server,
            "path": mount_path,
            "version": "v2",
            "auth": {
                "jwt": {
                    "path": auth_mount,
                    "role": role,
                    "kubernetesServiceAccountToken": {
                        "serviceAccountRef": _sa_ref(sa_name, sa_namespace),
                        "audiences": list(audiences),
                    },
                }
            },
        }
    }
    return _store(name, provider, api_version)


def gcpsm_store(
    *,
    name: str,
    project_id: str,
    cluster_name: str,
    cluster_location: str,
    cluster_project_id: str,
    sa_name: str = DEFAULT_ESO_SA,
    sa_namespace: str = DEFAULT_ESO_NAMESPACE,
    api_version: str = DEFAULT_API_VERSION,
) -> ClusterSecretStore:
    """A store that syncs to GCP Secret Manager, authenticating via Workload Identity.

    No key is stored: ESO's service account is bound to a GCP identity through
    Workload Identity. ``project_id`` is the fallback project for short secret
    names; fully qualified ``projects/<p>/secrets/<n>`` remote refs can target any
    project the identity can reach.
    """
    require(project_id, "project_id", context="gcpsm_store")
    require(cluster_name, "cluster_name", context="gcpsm_store")
    require(cluster_location, "cluster_location", context="gcpsm_store")
    require(cluster_project_id, "cluster_project_id", context="gcpsm_store")
    provider = {
        "gcpsm": {
            "projectID": project_id,
            "auth": {
                "workloadIdentity": {
                    "clusterName": cluster_name,
                    "clusterLocation": cluster_location,
                    "clusterProjectID": cluster_project_id,
                    "serviceAccountRef": _sa_ref(sa_name, sa_namespace),
                }
            },
        }
    }
    return _store(name, provider, api_version)


def awssm_store(
    *,
    name: str,
    region: str,
    role: str | None = None,
    sa_name: str = DEFAULT_ESO_SA,
    sa_namespace: str = DEFAULT_ESO_NAMESPACE,
    api_version: str = DEFAULT_API_VERSION,
) -> ClusterSecretStore:
    """A store that syncs to AWS Secrets Manager, authenticating via IRSA (JWT).

    ESO assumes an IAM role through IRSA using its service-account token; no
    access key is stored. ``role`` is an optional role ARN to assume for
    cross-account access; omit it to use the service account's own role.
    """
    require(region, "region", context="awssm_store")
    aws: dict = {
        "service": "SecretsManager",
        "region": region,
        "auth": {"jwt": {"serviceAccountRef": _sa_ref(sa_name, sa_namespace)}},
    }
    if role:
        aws["role"] = role
    return _store(name, {"aws": aws}, api_version)


def external_secret(
    *,
    name: str,
    namespace: str,
    store_name: str,
    kv_path: str,
    secret_name: str,
    refresh_interval: str = "1h",
    api_version: str = DEFAULT_API_VERSION,
) -> dict:
    """An ExternalSecret that pulls one KV path from a store into a Kubernetes Secret.

    ``store_name`` is the ClusterSecretStore to read through; ``kv_path`` is the
    source path (a name, never a value); ``secret_name`` is the Kubernetes Secret
    ESO creates and owns. ``dataFrom.extract`` pulls every key at the path.
    """
    require(name, "name", context="external_secret")
    require(namespace, "namespace", context="external_secret")
    require(store_name, "store_name", context="external_secret")
    require(kv_path, "kv_path", context="external_secret")
    require(secret_name, "secret_name", context="external_secret")
    return {
        "apiVersion": f"{API_GROUP}/{api_version}",
        "kind": "ExternalSecret",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "refreshInterval": refresh_interval,
            "secretStoreRef": {"name": store_name, "kind": "ClusterSecretStore"},
            "target": {"name": secret_name, "creationPolicy": "Owner"},
            "dataFrom": [{"extract": {"key": kv_path}}],
        },
    }


def to_manifest(obj: ClusterSecretStore | dict) -> str:
    """Serialize a rendered resource to a YAML manifest string."""
    manifest = obj.manifest if isinstance(obj, ClusterSecretStore) else obj
    return yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False)
