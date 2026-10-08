# Deploying the Ladon backend

The backend is one long-running process (D14): it migrates the datastore, then
serves the API, the built UI, and the apply operations. This directory holds
everything a cluster needs to run it. Nothing here contains a real value; every
cluster name, registry, address, and address range is a placeholder to replace.

| Path | What it is |
| --- | --- |
| `gojira.yaml` (repository root) | The deploy contract. The deployment platform's generator reads it from the root and emits the Deployment and the Service. |
| `k8s/base/` (repository root) | The manifests that generator preserves: identity, the namespaced RBAC, the volume, and the network policies. |
| `k8s/cluster/` (repository root) | The cluster-scoped objects an operator applies once: the `ClusterRole`, and the `ClusterRoleBinding` the standalone path needs. The generator refuses cluster-scoped kinds in `k8s/base`; on that path the contract's `cluster_roles: [ladon]` has the platform make the binding. |
| `standalone/` | A plain Deployment and Service, for a cluster where that platform is not in use. |
| `standalone/front-door/` | The cloud front door: an external HTTPS load balancer with the provider's identity-aware proxy, a managed certificate, and an http-to-https redirect. Optional, and applied by hand. |

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

The cloud front door in `standalone/front-door/` does not change that. The
Service stays `ClusterIP`; the load balancer reaches the pods through a
container-native network endpoint group, which is to say it bypasses the
Service's virtual IP rather than being given one of its own. The Service is
there to be selected and annotated, not to be exposed.

## The front door

Optional, and one shape of proxy rather than the only one: the cloud
provider's external HTTPS load balancer with its identity-aware proxy in
front. Four manifests, each documented in its own file.

| File | What it is |
| --- | --- |
| `backendconfig.yaml` | The health check (`/healthz` on 8000) and `iap.enabled`. Bound through the Service's `cloud.google.com/backend-config` annotation. |
| `managedcertificate.yaml` | The TLS certificate, issued and renewed by the provider. |
| `frontendconfig.yaml` | The http-to-https redirect. |
| `ingress.yaml` | The load balancer: `gce` class, the reserved static address, the host, and the backend Service. |

Two annotations on `standalone/service.yaml` connect them to the workload:
`cloud.google.com/neg` and `cloud.google.com/backend-config`. Both are inert
on a cluster with no such load balancer.

They are not covered by `scripts/validate-deploy.sh`, deliberately: they are
provider custom resources whose definitions are not on every cluster, and
they carry placeholders that are not valid values for the fields they sit in.
The step-by-step, including the address reservation, the DNS record, the IAP
grant, and the `identity.assertion` config that follows, is in
[../../docs/deployment-order.md](../../docs/deployment-order.md).

## The NetworkPolicy is required, not optional

A ClusterIP Service is still reachable from every pod on the cluster, which is
not the boundary described above. `k8s/base/networkpolicy.yaml` is what closes
it, in three parts:

1. `ladon-default-deny` denies all traffic, in and out, for every pod in the
   `ladon` namespace.
2. `ladon-backend` admits ingress to the backend from three named places and
   nothing else: a namespace labelled `ladon.dev/proxy=true` (an in-cluster
   proxy), the cloud load balancer's published address ranges (the front
   door), and a namespace labelled `ladon.dev/caller=true` (a machine caller
   on the service lane). It permits egress only to DNS, to OpenBao, to
   outbound https, and to the Kubernetes API server.
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

A third label is optional, and only for a deployment with a machine caller on
the service lane (`POST /api/service/secrets`). That caller runs in a
namespace of its own, which matches neither the proxy rule nor a
same-namespace selector, so its namespace is admitted the same deliberate
way:

```
kubectl label namespace <caller-namespace> ladon.dev/caller=true
```

The label opens the port. It grants no identity: the caller still
authenticates on the service lane's own terms.

### A cloud load balancer comes from no namespace

Where the front door is the cloud provider's own external HTTPS load balancer
rather than an in-cluster proxy, the `ladon.dev/proxy=true` rule admits
nothing, and this is the way it is found out: the backend service sits
permanently UNHEALTHY and the load balancer answers 502 with the pod running
and healthy.

The reason is that traffic and health checks alike arrive from the provider's
own infrastructure, direct to the pod through a container-native network
endpoint group, from fixed address ranges rather than from a namespace. On
Google those are `130.211.0.0/22` and `35.191.0.0/16`, and `ladon-backend`
carries an `ipBlock` rule for the pair. They are Google's published
constants, not a deployment's addresses, so unlike the rules below there is
nothing to replace. The front-door manifests are in
[`standalone/front-door/`](standalone/front-door/).

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

The `ClusterRole` (`k8s/cluster/clusterrole.yaml`) grants only what cannot be
namespaced:
`ClusterSecretStore` (a cluster-scoped kind), `ExternalSecret` in any namespace
(onboarded applications live in their own), `namespaces` and
`customresourcedefinitions` reads for the API-version probe, and `deployments`
reads to detect an operator already installed.

The one `delete` is on `ExternalSecret`, and it exists because the machine lane
removes the object it created when an application is taken down; leaving it
behind means an operator reconciling a KV path that is gone on purpose. It is
not granted on `ClusterSecretStore`: a store is an application's identity on
the cluster, and removing one is off-boarding rather than a per-secret call.

That grant reaches further than the kind it names. Ladon renders every
ExternalSecret with `creationPolicy: Owner`, so the Kubernetes Secret ESO
creates carries an owner reference back to it, and deleting the ExternalSecret
has the garbage collector delete the Secret with it. Deleting one custom
resource therefore takes a running workload's Secret away, in any namespace,
without any grant on Secrets outside `ladon`. It is what taking an application
down is supposed to do, and it is why two checks stand in front of it in the
code: the live object must record `ladon` as one of its field managers, and it
must belong to the application and namespace the caller named.

The `ClusterRole` is not part of `k8s/base`, and that is the deployment
platform's rule rather than ours: a base that carries a cluster-scoped kind is
refused, so an application cannot grant itself cluster-wide access by
committing a file. An operator applies the `ClusterRole` once, puts its name on
the platform's allowlist, and `gojira.yaml` names it under `cluster_roles`; the
platform then creates and owns the binding to the `ladon` ServiceAccount. The
standalone path has no platform to do that, so `k8s/cluster/` also carries the
`ClusterRoleBinding` for an operator to apply alongside.

Neither role can read a Secret outside the `ladon` namespace. A cluster-wide
Secret read would turn this service account into a way to collect every synced
value on the cluster, which is the opposite of the point.

## Applying it

```
# The cluster-scoped ClusterRole, once per cluster (an operator; on the
# deployment platform also add `ladon` to its cluster-role allowlist):
kubectl apply -f k8s/cluster/clusterrole.yaml

# The preserved manifests (identity, namespaced RBAC, volume, network policies):
kubectl apply -k k8s/base

# On a cluster without the deployment platform, the binding and the workload too:
kubectl apply -f k8s/cluster/clusterrolebinding.yaml
kubectl apply -f deploy/ladon/standalone/
```

`scripts/validate-deploy.sh` checks both without applying anything. The bring-up
order, including the vault, the bootstrap Secret, and a first look through a
port-forward, is in
[docs/deployment-order.md](../../docs/deployment-order.md).
