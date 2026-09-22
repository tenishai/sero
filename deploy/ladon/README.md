# Deploying the Ladon backend

The backend is one long-running process (D14): it migrates the datastore, then
serves the API, the built UI, and the apply operations. This directory holds
everything a cluster needs to run it. Nothing here contains a real value; every
cluster name, registry, address, and address range is a placeholder to replace.

| Path | What it is |
| --- | --- |
| `gojira.yaml` | The deploy contract. The deployment platform's generator reads it and emits the Deployment and the Service. |
| `k8s/base/` | The manifests that generator preserves: identity, RBAC, the volume, and the network policies. |
| `standalone/` | A plain Deployment and Service, for a cluster where that platform is not in use. |

Both paths read the same ConfigMap, Secret, and PersistentVolumeClaim, so an
instance can move from one to the other without the application noticing.

## What an operator creates

Two objects are created out of band, because neither can come from the platform
itself.

### The bootstrap Secret, `ladon-bootstrap`

Two keys, both consumed as environment variables:

| Key | What it holds |
| --- | --- |
| `LADON_DATABASE_URL` | The datastore. `sqlite:////data/ladon.db` for a single-node development instance (four slashes: the fourth is the leading slash of an absolute path), or a PostgreSQL URL for anything shared. |
| `BAO_TOKEN` | The token the backend authenticates to OpenBao with. |

This is the one chicken-and-egg in the deployment: `BAO_TOKEN` is what lets
Ladon reach OpenBao, so it cannot be delivered by OpenBao. It is created once,
by hand, by the operator who initialized the vault. Nothing else in the
deployment holds a secret value: every other secret lives in OpenBao and is
never written to the cluster by Ladon.

The token is never read from the config file, and a config that names one is
rejected.

### The ConfigMap, `ladon-config`

One key, `config.yaml`, holding the deployment config the backend reads through
`LADON_CONFIG` (which both deploy paths set to `/etc/ladon/config.yaml`). It is
the same file shape as `config/example.yaml` in the repository root: the KV
mount, the teams, the identity header, and the access lists that decide who
holds which namespace and who is an admin.

It is a ConfigMap and not a Secret because it holds no values, only names. If
you find yourself wanting to put a credential in it, that credential belongs in
OpenBao.

## The Service is ClusterIP, and that is not an oversight

`standalone/service.yaml` is `type: ClusterIP`, with no LoadBalancer and no
NodePort, and the generated Service is the same.

The reason is the trust boundary in [docs/backend.md](../../docs/backend.md):
there is no login form. A proxy in front of the platform authenticates the
person and passes the result in a header, and the backend trusts that header.
Anything that can reach the backend directly can set that header, and therefore
can be anyone, including an admin. Exposing the Service to the network would
hand out that ability.

So exposure is the proxy's job. The backend is reachable from the proxy and
from nothing else.

## The NetworkPolicy is required, not optional

A ClusterIP Service is still reachable from every pod on the cluster, which is
not the boundary described above. `k8s/base/networkpolicy.yaml` is what closes
it, in three parts:

1. `ladon-default-deny` denies all traffic, in and out, for every pod in the
   `ladon` namespace.
2. `ladon-backend` admits ingress to the backend on port 8000 **only** from a
   namespace labelled `ladon.dev/proxy=true`, and permits egress only to DNS,
   to OpenBao, and to the Kubernetes API server.
3. `ladon-openbao` re-admits the backend to OpenBao, which part 1 also closed.

Label the proxy's namespace before applying, or the backend is unreachable:

```
kubectl label namespace <proxy-namespace> ladon.dev/proxy=true
```

A label rather than a namespace name, because the namespace an ingress
controller lives in differs per cluster, and labelling is a deliberate act by
whoever owns that namespace rather than a name this repository has to guess.

One placeholder needs replacing per cluster: the egress rule for the API server
names an address range, because a control plane endpoint is not a pod and
cannot be selected. The committed value is the documentation-reserved range
from RFC 5737 and routes nowhere. A NetworkPolicy with a wrong CIDR is accepted
by the API server and silently breaks the cluster-apply layer, so check it.

## RBAC

Two roles, split by what actually needs cluster scope.

The namespaced `Role` covers Secrets, ConfigMaps, and ServiceAccounts in the
`ladon` namespace. The ServiceAccounts are the per-app identities ESO
authenticates as: D18a puts them in Ladon's own namespace rather than the
operator's, so Ladon never writes into a namespace another team owns.

The `ClusterRole` grants only what cannot be namespaced:
`ClusterSecretStore` (a cluster-scoped kind), `ExternalSecret` in any namespace
(onboarded applications live in their own), `namespaces` and
`customresourcedefinitions` reads for the API-version probe, and `deployments`
reads to detect an operator already installed.

Neither role can delete anything, and neither can read a Secret outside the
`ladon` namespace. A cluster-wide Secret read would turn this service account
into a way to collect every synced value on the cluster, which is the opposite
of the point.

## Applying it

```
# The preserved manifests (identity, RBAC, volume, network policies):
kubectl apply -k deploy/ladon/k8s/base

# On a cluster without the deployment platform, the workload too:
kubectl apply -f deploy/ladon/standalone/
```

`scripts/validate-deploy.sh` checks both without applying anything. The bring-up
order, including the vault, the bootstrap Secret, and a first look through a
port-forward, is in
[docs/deployment-order.md](../../docs/deployment-order.md).
