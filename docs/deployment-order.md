# Deployment order

This document describes the order in which a Ladon deployment is brought up, from
an empty cloud project to a working platform that teams can use. It is the
sequence Ladon's agent-driven apply model (D3 in [design.md](design.md))
orchestrates, and it follows from the baseline in [baseline.md](baseline.md).

The order matters: most phases have hard prerequisites on earlier ones, and a
phase started before its prerequisite is met fails, sometimes silently. Ladon's
job is to encode this sequence and check each prerequisite before acting, rather
than leaving an operator to run steps in the right order by hand. Every write is
dry-run first: a plan or diff against live state, reviewed, then applied.

## The three apply layers

Each phase below is owned by one of the three layers from [design.md](design.md):

- **Substrate** (Terraform): cloud resources. Cluster, key management, load
  balancing, private connectivity, observability, backup jobs.
- **Install** (direct Helm): the operated software. OpenBao and the External
  Secrets Operator, each a pinned chart.
- **Config** (OpenBao SDK/API and Kubernetes apply): namespaces, auth methods,
  policies, KV mounts, and the sync resources. This is also where the runtime
  onboarding of clusters and teams happens.

A small number of steps are one-time manual operator actions (initializing
OpenBao and recording its recovery material); those are called out explicitly.

## Phases

### 0. Cloud foundation (prerequisite, outside Ladon)

The cloud projects, the network and its subnets, and the cluster-fleet grouping
exist before Ladon runs. Ladon does not create these; it verifies they are
present and reachable. This is deliberately out of scope so Ladon stays portable
across organizations that already manage their cloud foundation their own way.

### 1. Cloud substrate (Substrate)

Provision the cluster (private, with workload identity), its node pools, a cloud
KMS key for auto-unseal, and internal load balancing for the OpenBao endpoint.

Prerequisite to watch: the identity that the cluster runs as must be granted use
of the KMS key. In the source this grant lived outside the substrate definition,
so a missing grant let the cluster come up but silently fail to auto-unseal.
Ladon checks the grant as part of this phase.

### 2. OpenBao install (Install)

Install OpenBao from its pinned Helm chart: Raft high availability with several
replicas, cloud-KMS auto-unseal, TLS from a cluster secret, and any required auth
plugin staged at startup. Where the install stages a plugin binary, its checksum
is verified rather than trusted; the source hardcoded a checksum in a runbook and
never validated it.

Prerequisite: phase 1 (cluster, KMS key and its grant, load balancer).

### 3. Initialize and unseal (manual, one-time)

An operator runs the one-time initialization, which produces the recovery key
shares and the initial root token, and records them safely. With cloud-KMS
auto-unseal configured in phase 2, the cluster unseals itself on restart
thereafter; the recovery shares are the break-glass path if KMS is unavailable.
Any auth plugin is registered here.

This is the one irreducibly manual step. Ladon guides it and verifies the result
(initialized, unsealed, reachable) but never handles the recovery material or the
root token beyond what the operator supplies for the next phase.

### 4. Platform root configuration (Config)

With a reachable, unsealed OpenBao, apply the baseline root configuration in
order: the platform namespaces, then the auth methods (the human operator OIDC
method and the workload JWT method, per [baseline.md](baseline.md)), then the KV
v2 secret engine. AppRole is enabled here only if the deployment uses it.

Prerequisite: phase 3 (initialized and unsealed, with a token that can configure
the root namespace). This is the first phase rendered by the config layer
(policies, teams, auth) and applied through the SDK with a diff first.

### 5. Observability (Substrate)

Install the metrics and dashboard stack and point it at OpenBao's telemetry.
Independent of the config phases; it needs only a running OpenBao to scrape.

### 6. External Secrets Operator on the platform cluster (Install)

Install ESO from its pinned chart on the platform cluster, and create the secret
stores it needs: the cross-namespace read store, and one store per enabled cloud
secret-manager target. ESO does all secret syncing and watching; Ladon installs
and configures it but builds no custom sync worker.

Pin ESO to one version across the fleet. The source ran the install at one
version while some clusters carried a different, separately managed version,
which produced custom-resource API mismatches; Ladon treats the ESO version as a
single fleet-wide pin (see [versions.md](versions.md)).

An operator that is already on the cluster is reused, not replaced. ESO is
cluster-scoped and singular, so two operators fight over one set of custom
resource definitions; before installing anything Ladon looks for a deployment
labelled `app.kubernetes.io/name=external-secrets` in any namespace, and when it
finds one it records that operator's version and namespace and runs no helm
command at all. You can tell which happened from the install report: it either
reads `reusing external-secrets <version> in namespace <namespace>`, or names the
pinned chart version it installed. To check by hand, run `kubectl get deploy -A
-l app.kubernetes.io/name=external-secrets`; Ladon reads the version from the
deployment's `app.kubernetes.io/version` label, falling back to its image tag.
Reuse means the fleet-wide pin above is not enforced on that cluster, so confirm
the reported version is one your stores and ExternalSecrets support.

Prerequisite: phase 4 (the root auth method and cross-namespace read policy ESO
authenticates against).

### 7. Private connectivity (Substrate)

Establish private connectivity from each consumer network to the OpenBao
endpoint, with private name resolution so workloads resolve the endpoint to a
private address. This must exist before a workload on any team cluster can reach
OpenBao.

Prerequisite: phase 2 (the OpenBao endpoint to connect to). Required before
phase 9 and 10 on any given team cluster.

### 8. Register a team cluster (Config, runtime)

Register a team's cluster so the platform can reach it and trust its identity
tokens. Registration yields administrative access to the cluster and the
cluster's token issuer URL, which the workload JWT auth uses.

This and the phases after it are runtime operations: clusters are registered over
time, not all at bring-up. They are the "agents deploy and maintain state"
operations, each driven through the agent/tool surface, each dry-run first.

### 9. ESO on the team cluster (Install, runtime)

Install ESO (the same fleet-wide pinned version) on the registered team cluster.
As in phase 6, an operator already running on that cluster is detected, reported
and reused rather than reinstalled; a team cluster is the likely place to find
one somebody else owns.

```
scripts/ladon install-eso                      # dry-run
scripts/ladon install-eso --apply              # install, or reuse and report
scripts/ladon install-eso --namespace NS --apply
```

The command runs against whatever cluster the current `kubectl` context points
at. When it reuses an operator it also reports which custom-resource version
that cluster stores, which is the version the later phases render at; the
`--namespace` flag applies only to an install, since a reused operator stays
where it is.

Prerequisites: phase 8 (registered, reachable cluster) and phase 7 (private
connectivity from that cluster to OpenBao).

### 10. Onboard a team on a cluster (Config, runtime, idempotent)

Wire one team to one cluster. On OpenBao: enable the workload JWT mount for that
cluster in the team's namespace, point it at the cluster's token issuer, and
create the team's read policy and the role bound to the ESO workload identity. On
the cluster: apply the team's secret store so its ExternalSecrets can read from
the team namespace. Designed to be idempotent and safe to re-run.

An application is onboarded the same way, one level down (D17), and both of its
halves are applied by one command: the OpenBao policy and role first, then the
service account and the store on the cluster. The cluster half is never applied
when the OpenBao half did not land, since a store pointing at a role that does
not exist fails later, at refresh time, somewhere else. Re-running reports every
object and every document already current and writes nothing.

```
scripts/ladon apply --phase app-onboarding --config deployment.yaml \
  --team TEAM --app APP --cluster CLUSTER --k8s-namespace NAMESPACE --apply
```

Applying refuses outright on a cluster with no operator: nothing would read the
store, so phase 9 comes first. The store is rendered at the custom-resource
version that cluster stores, probed at apply time.

One `ExternalSecret` per secret then carries a path into the Secret a workload
already reads, through the store the onboarding created. The path must be inside
the application's own prefix, which is all its identity can read.

```
scripts/ladon apply --phase sync --team TEAM --app APP \
  --path apps/APP/PURPOSE --name NAME \
  [--k8s-namespace NAMESPACE] [--target-name SECRET] \
  [--refresh-interval 1h] --apply
```

`--k8s-namespace` defaults to the application name, as onboarding does, so an
application with a namespace of its own needs no flag. Naming a namespace the
application's store does not admit is not refused here: which namespaces may use
a store is the store's to say, so the document applies and then fails at refresh
time, in the operator's logs, with no Secret created. `--target-name` is the
Kubernetes Secret ESO creates, which is the name the workload already uses when
it differs from the ExternalSecret's; `--refresh-interval` is how often ESO
re-reads the path, defaulting to `1h`.

The dry-run names the path and the Secret each document would fill, so what is
about to be wired is reviewable before it is:

```
  [cluster] create ExternalSecret/NAMESPACE/NAME (apps/APP/PURPOSE -> Secret/SECRET)
```

Prerequisites: phase 9 (ESO on the cluster) and the team's namespace from
phase 11.

### 11. Team namespace request (Config plus workflow)

A team requests a namespace through the backend. The request is recorded as
operational state, optionally routed through a ticketing hook, and turned into a
namespace plus its baseline policies, auth roles, and KV mount through the config
layer. An empty platform (zero teams) is valid; teams are added entirely from
configuration over time.

## Platform cluster, single node

The phases above describe the full sequence, across clouds and clusters and
teams. A platform cluster that operates one dedicated OpenBao does not need all
of it, and bringing one up should not require reading all of it either. This is
the short path: one cluster, one namespace, one node's worth of vault, and the
backend running beside it.

It is the D13a shape. Raft on a single node, no HA. Unseal is Shamir out of the
box and cloud KMS by layering one rendered values overlay (step 2a); more
replicas come later by configuration. Nothing in the consumer contract depends
on either.

Everything below runs in one namespace, `ladon`, and every angle-bracketed
value is a placeholder.

### 1. The namespace

```
kubectl create namespace ladon
```

### 2. OpenBao, from the pinned chart

The base values plus the dev overlay: single node, Shamir, small volumes. Use
the exact chart version from [versions.md](versions.md); never a floating
chart.

```
helm repo add openbao https://openbao.github.io/openbao-helm
helm repo update openbao

helm upgrade --install ladon-openbao openbao/openbao \
  --version <pinned-chart-version> \
  --namespace ladon \
  -f deploy/openbao/values/values-base.yaml \
  -f deploy/openbao/values/values-dev.yaml \
  --dry-run

# Same command without --dry-run once the render is what you expect.
```

One release per cluster, and the release name matters. The chart creates a
cluster-scoped ClusterRoleBinding named after the release, and cluster-scoped
objects have no namespace to keep two copies apart. A second install of the
same chart under the same release name in another namespace fails on ownership,
because the object already exists and belongs to the first release. So: exactly
one release named `ladon-openbao` per cluster. If you need a second vault on
the same cluster, give it a different release name and expect a second
ClusterRoleBinding.

### 2a. Auto-unseal from a cloud KMS

Shamir means a person unseals the vault by hand after every restart, including
the restart nobody was watching. Point the deployment at a cloud KMS key and the
vault unseals itself. This is the shape the platform vault runs in; do it here
rather than promising to do it later.

Name the key in the deployment config. References only, never key material:

```
deployment:
  kms:
    provider: gcpckms
    project: <project>
    region: <kms-location>
    key_ring: <kms-keyring>
    crypto_key: <kms-key>
```

`awskms` and `azurekeyvault` take their own fields; see
[config/example.yaml](../config/example.yaml) for all three shapes.

Render the values overlay that carries the seal stanza, and the annotation that
binds the vault's service account to the identity the key is granted to:

```
scripts/ladon seal-values \
  --config <deployment-config.yaml> \
  --base-config deploy/openbao/values/values-dev.yaml \
  --service-account-annotation iam.gke.io/gcp-service-account=<unseal-sa>@<project>.iam.gserviceaccount.com \
  > seal-values.yaml
```

The command prints the overlay and nothing else, so it goes straight to a file
or through a review. `--base-config` is the values file whose
`server.ha.raft.config` the overlay extends (a plain `.hcl` file is accepted
too): a Helm value is replaced rather than merged, so the overlay has to carry
the whole server config, and rendering it from the file you already install with
is what keeps the two from drifting. A deployment whose provider is `shamir` is
refused with exit 2, because there is no stanza to render.

Install with the overlay last, so it wins:

```
helm upgrade --install ladon-openbao openbao/openbao \
  --version <pinned-chart-version> \
  --namespace ladon \
  -f deploy/openbao/values/values-base.yaml \
  -f deploy/openbao/values/values-dev.yaml \
  -f seal-values.yaml
```

Two IAM roles for the unseal identity, both on the key and neither on the
project:

- `roles/cloudkms.cryptoKeyEncrypterDecrypter`, to wrap and unwrap the root key.
- `roles/cloudkms.viewer`, because the `gcpckms` seal calls `cryptoKeys.get` at
  startup to check the key exists. Encrypt and decrypt do not grant that read,
  and without it the vault logs `error checking key existence` and never
  unseals.

[terraform/modules/unseal-key/gcp](../terraform/modules/unseal-key/gcp) applies
the key and both grants as one unit.

One network rule, already in
[the manifests](../deploy/ladon/k8s/base/networkpolicy.yaml) and worth knowing
about: workload identity fetches the pod's credentials from the node metadata
server at `169.254.169.254` over plain TCP 80. Under the default deny, the rule
next to it opens that address on port 53 for DNS only, so without the port 80
rule the vault crash-loops at startup and says nothing about the network. It is
on the vault's policy alone; the backend holds no cloud identity.

### 3. Initialize and unseal (manual, one-time)

This is phase 3 above, unchanged and still the one irreducibly manual step.

```
kubectl -n ladon exec -it ladon-openbao-0 -- bao operator init
kubectl -n ladon exec -it ladon-openbao-0 -- bao operator unseal
```

The second command is the Shamir path, and with Shamir the pod needs unsealing
again after every restart. With a seal stanza from step 2a, `bao operator init`
returns recovery keys instead of unseal keys and the vault is already unsealed:
skip the `unseal` command, then and after every restart.

Record the recovery shares and the initial root token the way you would any
break-glass material. They do not belong in the cluster, in this repository, or
in a terminal scrollback you keep.

### Migrating a running Shamir vault to KMS

A vault that is already initialized and holding data does not have to be rebuilt
to change its seal. The migration runs in place, and this is the sequence that
worked on the platform vault.

Take a snapshot first. It is the way back.

```
kubectl -n ladon exec ladon-openbao-0 -- bao operator raft snapshot save /tmp/pre-kms.snap
kubectl -n ladon cp ladon-openbao-0:/tmp/pre-kms.snap ./pre-kms.snap
```

1. Render the overlay and `helm upgrade` with it, exactly as in step 2a. It adds
   the seal stanza and the service-account annotation together; an annotation
   that did not land shows up as the same crash loop as a missing grant.
2. Delete the pod. The chart's StatefulSet updates `OnDelete`, so a values
   change restarts nothing by itself and the old config keeps serving until you
   say otherwise.

   ```
   kubectl -n ladon delete pod ladon-openbao-0
   ```

3. The vault comes back in seal-migration mode. `bao status` reports
   `migration: true` and exits 2 while it is sealed, so that exit code is the
   expected state here and not a failure to chase.
4. Unseal once per threshold share, each with `-migrate`:

   ```
   kubectl -n ladon exec -it ladon-openbao-0 -- bao operator unseal -migrate <shamir-share>
   ```

   The Shamir shares become the recovery keys. Keep them exactly as carefully as
   before: they are the break-glass path when the KMS key is unavailable.
5. Prove it unseals itself. Delete the pod once more and watch it come back
   ready with nobody typing anything.

   ```
   kubectl -n ladon delete pod ladon-openbao-0
   kubectl -n ladon exec ladon-openbao-0 -- bao status
   ```

### DNS under the default deny

Check this before applying the network policies in the next step. Read
`/etc/resolv.conf` in any pod on the cluster. If the nameserver is a
link-local address (for example a cloud provider's own DNS at
`169.254.169.254`), the kube-dns pod selector in the policies matches
nothing, and the manifests' link-local rule is what keeps DNS working for the
vault and the backend. Without DNS the vault cannot fetch the workload token
issuer's keys, and every store reports that it could not create a client.

### 4. The backend's own objects

Identity, RBAC, the data volume, and the network policies:

```
kubectl apply -k deploy/ladon/k8s/base
```

Then label two namespaces, or the policies keep out the two things that must
get in:

```
kubectl label namespace <proxy-namespace> ladon.dev/proxy=true
kubectl label namespace <eso-namespace>   ladon.dev/eso=true
```

Without the first the backend is unreachable; without the second every
`ExternalSecret` on the cluster fails, because the operator runs in its own
namespace and cannot reach OpenBao.

The API-server and https egress rules carry placeholders: the first is an
address range that routes nowhere, the second is deliberately wide. Replace
both before the cluster-apply layer is used. A wrong range is accepted by the
API server and is simply wrong, so that layer hangs rather than failing
loudly. See [../deploy/ladon/README.md](../deploy/ladon/README.md).

### 5. The config and the bootstrap Secret

The ConfigMap holds names only, so it can be created from the example and
edited. On this path the backend mounts it as a file, so the key is
`config.yaml`:

```
kubectl -n ladon create configmap ladon-config \
  --from-file=config.yaml=config/example.yaml
```

On a cluster whose deployment platform delivers the ConfigMap as environment
variables rather than a volume, the key is `LADON_CONFIG_YAML` instead and the
backend reads the config inline. That difference, and why it exists, is in
[../deploy/ladon/README.md](../deploy/ladon/README.md).

While you are editing the config: if your proxy signs what it asserts, set
`identity.assertion` so the backend verifies the signature rather than trusting
the header's text. The network policy is what stops anything but the proxy
reaching the backend; a verified assertion is what stops a misconfigured
network policy from being the only thing standing between a caller and any
identity they name. Set both.

The Secret holds the two values the backend cannot get from anywhere else: the
datastore URL, and the token that lets it reach OpenBao.

```
kubectl -n ladon create secret generic ladon-bootstrap \
  --from-literal=LADON_DATABASE_URL=sqlite:////data/ladon.db \
  --from-literal=BAO_TOKEN="$(tr -d '\n' < <path-to-a-file-holding-the-token>)"
```

Note the `tr`. `--from-file` would store the file's bytes exactly, and almost
every editor leaves a trailing newline, which becomes part of the token and
makes every OpenBao call fail authentication for a reason nothing reports
clearly. Use `--from-literal`, or strip the newline as above.

Delete the file afterwards. The token is the one credential in the deployment
that OpenBao cannot issue to itself.

### 6. The backend

On a cluster running the deployment platform, `deploy/ladon/gojira.yaml` is the
whole of it: the generator emits the Deployment and Service from that contract,
and the manifests from step 4 are preserved alongside.

On a cluster without it, apply the standalone pair. Set the image to the tag the
image workflow pushed (the short commit sha) and `BAO_ADDR` to where OpenBao
answers, then:

```
kubectl apply -f deploy/ladon/standalone/
kubectl -n ladon rollout status deployment/ladon
```

The image migrates the datastore before it serves, so a schema that cannot be
brought to head is a pod that exits rather than one that answers against a
schema it does not trust.

This is the path a shared development cluster with no deployment platform of
its own actually runs: the standalone pair, not the generated one.

### 6a. The front door

Optional, and the step that turns a port-forward into a service people can
use. This is the cloud provider's own external HTTPS load balancer with its
identity-aware proxy in front, which is the proxy the backend has been
assuming all along. The manifests are in
[../deploy/ladon/standalone/front-door](../deploy/ladon/standalone/front-door)
and each carries its reasoning; this is the order to do things in, which is
what the manifests cannot say.

They are not covered by `scripts/validate-deploy.sh`, so nothing checks them
before they meet a cluster. Read what you apply.

**1. Reserve the address.** Global, external, and reserved rather than
ephemeral, because an ephemeral address changes when the load balancer is
recreated and takes the DNS record with it.

```
gcloud compute addresses create <static-ip-name> --global
gcloud compute addresses describe <static-ip-name> --global --format='value(address)'
```

The Ingress names the address by *name*, not by value. A name that was never
reserved is not an error: the Ingress gets an ephemeral address instead and
says nothing.

**2. Point DNS at it.** An A record for `<hostname>` holding the address the
previous command printed. Do this before applying, not after: the managed
certificate is validated over that name, so it stays `Provisioning` until the
record resolves here, and a certificate that was asked for too early sits in
`FailedNotVisible` until the record catches up.

**3. Apply the manifests.** The Service first, because its two annotations
are what bind the rest to the workload.

```
kubectl apply -f deploy/ladon/standalone/service.yaml
kubectl apply -f deploy/ladon/standalone/front-door/
```

Replace the two placeholders first: the address name and the host in
`ingress.yaml`, and the same host in `managedcertificate.yaml`. Then wait.
Ten to twenty minutes for the load balancer, and up to an hour for the
certificate, is ordinary.

```
kubectl -n ladon describe ingress ladon
kubectl -n ladon describe managedcertificate ladon
```

The failure to expect here is a 502 with a healthy pod. Two causes, both
already handled in this repository and both worth recognising:

- The health check. The default checks `/` on the Service port, and a
  deployment serving the API alone has no `/`. `backendconfig.yaml` points it
  at `/healthz` on 8000, which is unauthenticated because a probe carries no
  identity.
- The NetworkPolicy. Traffic and health checks arrive from the load
  balancer's own ranges, `130.211.0.0/22` and `35.191.0.0/16`, direct to the
  pod through a container-native network endpoint group. They come from no
  namespace, so the `ladon.dev/proxy=true` label admits none of it;
  `ladon-backend` carries an `ipBlock` rule for the pair instead. A cluster
  running an older copy of
  [the manifests](../deploy/ladon/k8s/base/networkpolicy.yaml) will fail here
  until it is reapplied.

**4. Grant somebody access.** Enabling IAP authenticates callers; it
authorizes nobody. Until this grant exists, every person gets a sign-in page
and then a refusal, including the person who built the thing.

```
gcloud compute backend-services list --global
gcloud iap web add-iam-policy-binding \
  --resource-type=backend-services \
  --service=<backend-service-name> \
  --member='user:<email>' \
  --role='roles/iap.httpsResourceAccessor'
```

A group is the better member in anything but a first test.

**5. Verify what the proxy asserts.** IAP signs a JWT and puts it in
`x-goog-iap-jwt-assertion` alongside the plain email header. Configure the
backend to verify the signature rather than trust the text, in the same
deployment config from step 5:

```yaml
identity:
  assertion:
    header: x-goog-iap-jwt-assertion
    jwks_url: https://www.gstatic.com/iap/verify/public_key-jwk
    issuer: https://cloud.google.com/iap
    audience: /projects/<project-number>/global/backendServices/<backend-service-id>
    email_claim: email
```

The first three values are Google's, identical in every deployment. The
audience is yours, and it is the one field that has to be looked up. Its two
parts:

```
gcloud projects describe <project> --format='value(projectNumber)'
gcloud compute backend-services describe <backend-service-name> --global --format='value(id)'
```

The project *number*, not the project id, and the backend service's numeric
`id`, not its name. The backend service is the one the Ingress created for
this Service; `gcloud compute backend-services list --global` names it. Get
the audience wrong and every request is `401 {"error": "no identity"}`, with
nothing to distinguish it from presenting no token at all, which is
deliberate. Get it right and the header lane is closed: an assertion cannot
be replayed at another service behind the same proxy.

**A warning about the lane you were using.** Once `identity.assertion` is
set it is *required* on every `/api` route, it decides the identity, and the
plain header is not read at all. The port-forward in step 7 below still
reaches the pod, and `/healthz` still answers, but every `/api` call over it
now returns 401, because nothing on that path mints an IAP token. This is
the point of setting it, not a side effect: it is what stops a misconfigured
NetworkPolicy from being the only thing between a caller and any identity
they care to name. Set it knowing that operator access from then on goes
through the front door, and keep a way in that does not depend on the front
door being healthy.

### 7. A first look

The Service is ClusterIP and the NetworkPolicy admits only the proxy, so the
way in before a proxy exists is a port-forward:

```
kubectl -n ladon port-forward svc/ladon 8000:8000
curl -s http://127.0.0.1:8000/healthz
```

A port-forward reaches the pod through the API server rather than across the
pod network, so it is not blocked by the ingress policy. That is a property of
port-forwarding, not a hole in the policy: it requires cluster credentials that
already permit far more.

`/healthz` is unauthenticated, because a probe carries no identity. Every `/api`
route wants the identity header, which is what the proxy supplies in a real
deployment; on a port-forward, set it yourself to see what an operator sees.

That last sentence stops being true the moment `identity.assertion` is
configured (step 6a). A header you set by hand is then ignored, and every
`/api` call over a port-forward is 401. `/healthz` still answers, so this
remains the way to tell a broken pod from a broken front door.

### 8. Onboarding an app through the endpoint

App onboarding (step 10 above, in its full-fleet form) does not have to run
from the CLI on this path. With the backend up, an admin can start the same
phase as a background job over the API it just started serving:

```
curl -X POST http://127.0.0.1:8000/api/admin/apply/app-onboarding \
  -H 'content-type: application/json' \
  -d '{"team": "TEAM", "app": "APP", "cluster": "CLUSTER", "apply": true}'
```

The call answers `202` with a job id; `GET /api/admin/jobs/<id>` is what a
caller polls for the op-by-op report. See
[backend.md](backend.md#apply-operations-as-jobs) for the job lifecycle, the
one-job-per-kind rule, and how a namespace-request approval starts the same
kind of run. This is how app onboarding was actually exercised on the
platform cluster: through the endpoint, not the CLI.

### What is deliberately not here

No ESO install (phase 6), no team clusters (phases 8 to 10), no private
connectivity (phase 7), no observability (phase 5). Add each when something
needs it. A platform cluster that only operates its own vault and backend needs
none of them on day one.

## Dependency summary

```
0 foundation
  └─ 1 substrate (cluster, KMS+grant, LB)
       └─ 2 OpenBao install
            ├─ 3 init + unseal (manual)
            │     └─ 4 root config (namespaces, auth, KV)
            │          ├─ 5 observability
            │          └─ 6 ESO on platform cluster
            └─ 7 private connectivity
                 └─ 8 register team cluster
                      └─ 9 ESO on team cluster
                           └─ 10 onboard team on cluster
                                └─ (needs 11 team namespace)
```

## What Ladon does differently

The source platform documented this order but did not enforce it: the substrate
and config phases were run as a series of manual applies per directory, with no
orchestration and several prerequisites left implicit (the KMS grant, the plugin
checksum, the fleet-wide ESO version, and the boundary between the two ways teams
were created). Each gap was a way for a deployment to end up half-configured.

Ladon's contribution is to make the order a first-class, dry-run-first plan: each
phase states its prerequisites, Ladon checks them against live state, shows the
diff, and applies only on confirmation. The sequence above is the backbone the
apply layers hang from.

## Running the apply CLI

The SDK config phases (4, 9 and 10) are runnable from a checkout with the `ladon`
CLI wrapper. It builds a plan and either renders it or applies it; every apply is
a dry-run unless `--apply` is passed. Both apply modes connect, because the
dry-run diffs the plan against live state and reports, per operation, whether it
would create, update, or find it already current. Use `plan` for the offline
render.

Phases with a cluster half (`app-onboarding`, `sync`) report it the same way,
one line per document, prefixed `[cluster]`:

```
Cluster documents (applied):
  [cluster] create ServiceAccount/ladon/eso-TEAM-APP-DIGEST
  [cluster] create ClusterSecretStore/ladon-TEAM-APP-DIGEST
```

A dry-run sends the documents to the API server as a server-side dry run, so the
report is what that cluster would really do, and a document another field
manager owns a field of is reported `blocked` with the conflict as its reason
rather than forced. `plan` is the offline render and connects to neither
OpenBao nor the cluster, so the documents it prints carry the current
custom-resource version rather than one probed from the target.

```
# Render a plan, no connection made:
scripts/ladon plan --phase root-config --jwt-discovery-url URL

# Phase 4 root config (dry-run, then apply):
scripts/ladon apply --phase root-config --jwt-discovery-url URL
scripts/ladon apply --phase root-config --jwt-discovery-url URL --apply

# Phase 10 per-team onboarding, scoped to the team namespace:
scripts/ladon apply --phase team-onboarding --cluster CLUSTER \
  --oidc-discovery-url URL --namespace TEAM --apply

# Phase 9 the operator, then phase 10 one application and one of its secrets:
scripts/ladon install-eso --apply
scripts/ladon apply --phase app-onboarding --config deployment.yaml \
  --team TEAM --app APP --cluster CLUSTER --k8s-namespace NAMESPACE --apply
scripts/ladon apply --phase sync --team TEAM --app APP \
  --path apps/APP/PURPOSE --k8s-namespace NAMESPACE --name NAME --apply
```

For either apply mode, the OpenBao address and token come from the environment
(`BAO_ADDR`/`VAULT_ADDR` and `BAO_TOKEN`/`VAULT_TOKEN`). The token is never
logged, and a placeholder value is refused.

A `--config` deployment file can supply the parts it already models, so they do
not have to be repeated as flags: the KV mount, and a team's namespace (resolved
from `--team`). Auth inputs stay explicit flags. An explicit `--kv-mount` or
`--namespace` overrides the config.

```
# KV mount taken from deployment.kv_mount in the config:
scripts/ladon plan --phase root-config --jwt-discovery-url URL --config deployment.yaml

# Team namespace resolved from the config's team list:
scripts/ladon apply --phase team-onboarding --cluster CLUSTER \
  --oidc-discovery-url URL --config deployment.yaml --team TEAM --apply
```

## Terraform apply flow (substrate, D10)

The cloud substrate is Terraform, and Terraform has a failure mode the config
layers do not: a clean `plan` does not guarantee a clean `apply`. Quota, IAM
propagation, state locks, drift, and provider bugs all surface only at apply.
So the substrate is never merged on the strength of a plan alone. The rule is
**apply first, prove it landed, then merge**, so the `main` branch only ever
holds Terraform that has actually been applied.

What runs where:

- **CI (every Terraform change, credential-free):** `terraform fmt -check` and,
  per module, `init -backend=false` + `validate`. This is `scripts/validate-terraform.sh`,
  the `terraform validate` job. It proves the configuration is well-formed; it
  does not need cloud credentials and does not run `plan` or `apply`.
- **The agent (with credentials and state):** runs `terraform plan` for review,
  then `terraform apply`. Consistent with the agent-driven, dry-run-first apply
  model (see [design.md](design.md) D3), there is no GitOps reconciler and no CI
  job that auto-applies or auto-merges. The agent owns the apply.
- **Merge:** a substrate change is merged only after its apply has succeeded
  against the target environment. The safety property (never merge an unproven
  apply) is held by this process, not by an auto-merge step.

`plan` and `apply` in CI, against a real project and remote state backend, are a
later step that depends on provisioning those cloud credentials; until then the
agent runs them out of band and CI covers the static checks.
