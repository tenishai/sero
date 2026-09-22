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

Both paths read the same Secret and PersistentVolumeClaim, and the same
ConfigMap under one differently named key (see below), so an instance can move
from one to the other with a single change.

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

Watch the trailing newline when you create it. `kubectl create secret
--from-file` stores the file's bytes exactly, and almost every editor and
`echo` leaves a newline at the end, which becomes part of the token and makes
every OpenBao call fail authentication for a reason nothing reports clearly.
Use `--from-literal`, or strip it:

```
kubectl -n ladon create secret generic ladon-bootstrap \
  --from-literal=LADON_DATABASE_URL=sqlite:////data/ladon.db \
  --from-literal=BAO_TOKEN="$(tr -d '\n' < <path-to-a-file-holding-the-token>)"
```

### The ConfigMap, `ladon-config`

The deployment config: the KV mount, the teams, the identity header, and the
access lists that decide who holds which namespace and who is an admin. It is
the same shape as `config/example.yaml` in the repository root. It is a
ConfigMap and not a Secret because it holds no values, only names. If you find
yourself wanting to put a credential in it, that credential belongs in
OpenBao.

**The two deploy paths deliver it differently, and the key name differs with
them.**

| Path | Key | How the backend reads it |
| --- | --- | --- |
| Generated (`gojira.yaml`) | `LADON_CONFIG_YAML` | Inline YAML in an environment variable. |
| Standalone | `config.yaml` | A mounted file, at `LADON_CONFIG=/etc/ladon/config.yaml`. |

The reason is that `config_from` in the deploy contract is an `envFrom` source,
not a volume. Every key in the ConfigMap becomes an environment variable, so a
key called `config.yaml` becomes an environment variable called `config.yaml`
and never becomes a file. Rather than fight that, the backend accepts the
config inline: when `LADON_CONFIG` is not set, it reads `LADON_CONFIG_YAML`.

For the generated path:

```
kubectl -n ladon create configmap ladon-config \
  --from-file=LADON_CONFIG_YAML=config/example.yaml
```

which produces a ConfigMap whose single key holds the file's text:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: ladon-config
  namespace: ladon
data:
  LADON_CONFIG_YAML: |
    deployment:
      kv_mount: kv
    # ... the rest of the config
```

`gojira.yaml` therefore does **not** set `LADON_CONFIG`. Setting it to a path
nothing mounts is the way to break that deployment: the backend would look for
a file that is not there instead of reading the variable.

For the standalone path, the key is `config.yaml` and it is mounted as a file:

```
kubectl -n ladon create configmap ladon-config \
  --from-file=config.yaml=config/example.yaml
```

An instance moving between the two paths needs this one key renamed. Nothing
else about the config changes.

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
2. `ladon-backend` admits ingress to the backend **only** from a namespace
   labelled `ladon.dev/proxy=true`, and permits egress only to DNS, to
   OpenBao, to outbound https, and to the Kubernetes API server.
3. `ladon-openbao` re-admits the backend and the External Secrets Operator to
   OpenBao, and gives OpenBao the egress that part 1 also closed.

### Two namespace labels to apply

```
kubectl label namespace <proxy-namespace> ladon.dev/proxy=true
kubectl label namespace <eso-namespace>   ladon.dev/eso=true
```

Without the first, the backend is unreachable. Without the second, every
`ExternalSecret` on the cluster fails: ESO runs in its own namespace, so a
same-namespace rule never matches it, and it cannot reach OpenBao to read the
values it syncs.

Labels rather than namespace names, because which namespace an ingress
controller or the operator lives in differs per cluster, and labelling is a
deliberate act by whoever owns that namespace rather than a name this
repository has to guess.

### OpenBao needs egress, not just ingress

The default deny covers egress, and OpenBao is not a passive server. Its JWT
auth method calls out to verify what a workload presented: it fetches the
cluster's OIDC discovery document and JWKS, which on this platform is an
external https endpoint rather than an in-cluster Service, and some auth
methods make a TokenReview-style call to the API server. A policy listing only
ingress leaves all of that blocked, and every authentication fails on a timeout
rather than on anything that names the cause. `ladon-openbao` therefore sets
`policyTypes: [Ingress, Egress]` and lists DNS, Raft peers, https, and the API
server.

The backend needs outbound https for the same kind of reason: it fetches the
proxy's JWKS to verify what the proxy asserted, and `helm repo add` reaches the
chart repositories the install layer pins.

### What to replace, per cluster

Every placeholder is marked `REPLACE` in the file. None of them fails loudly: a
NetworkPolicy with a wrong address range is accepted by the API server and is
simply wrong.

- **The https rules** are `0.0.0.0/0` on TCP 443. That is the deliberate wide
  default, because the issuer, JWKS, and chart-repository hosts are not
  knowable from here. Narrow each to the hosts your deployment uses.
- **The API-server rules** name an address range, because a control plane
  endpoint is not a pod and cannot be selected. **One rule per cluster
  endpoint**: a single range cannot cover several clusters, so a deployment
  that manages more than the cluster it runs on needs one `ipBlock` entry per
  endpoint. The committed value is the documentation-reserved range from
  RFC 5737 and routes nowhere.
- **Name the endpoint, not the Service.** In-cluster traffic to
  `kubernetes.default.svc` is DNAT'd from the Service ClusterIP to the real
  control plane address before most CNI plugins evaluate egress, so a rule
  naming the Service CIDR matches nothing. Use the control plane address. A
  few CNI plugins evaluate before DNAT instead; if egress to the API server is
  denied with the endpoint allowed, try the Service CIDR.
- **The backend's ingress ports** are 8000 and 8080, because the standalone
  Deployment takes the image's default 8000 and `gojira.yaml` sets
  `LADON_PORT` to 8080 for the generated one. Drop the port you do not use.

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
