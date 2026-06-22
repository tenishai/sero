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

Prerequisites: phase 8 (registered, reachable cluster) and phase 7 (private
connectivity from that cluster to OpenBao).

### 10. Onboard a team on a cluster (Config, runtime, idempotent)

Wire one team to one cluster. On OpenBao: enable the workload JWT mount for that
cluster in the team's namespace, point it at the cluster's token issuer, and
create the team's read policy and the role bound to the ESO workload identity. On
the cluster: apply the team's secret store so its ExternalSecrets can read from
the team namespace. Designed to be idempotent and safe to re-run.

Prerequisites: phase 9 (ESO on the cluster) and the team's namespace from
phase 11.

### 11. Team namespace request (Config plus workflow)

A team requests a namespace through the backend. The request is recorded as
operational state, optionally routed through a ticketing hook, and turned into a
namespace plus its baseline policies, auth roles, and KV mount through the config
layer. An empty platform (zero teams) is valid; teams are added entirely from
configuration over time.

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

The SDK config phases (4 and 10) are runnable from a checkout with the `ladon`
CLI wrapper. It builds a plan and either renders it or applies it; every apply is
a dry-run unless `--apply` is passed.

```
# Render a plan, no connection made:
scripts/ladon plan --phase root-config --jwt-discovery-url URL

# Phase 4 root config (dry-run, then apply):
scripts/ladon apply --phase root-config --jwt-discovery-url URL
scripts/ladon apply --phase root-config --jwt-discovery-url URL --apply

# Phase 10 per-team onboarding, scoped to the team namespace:
scripts/ladon apply --phase team-onboarding --cluster CLUSTER \
  --oidc-discovery-url URL --namespace TEAM --apply
```

For an apply, the OpenBao address and token come from the environment
(`BAO_ADDR`/`VAULT_ADDR` and `BAO_TOKEN`/`VAULT_TOKEN`). The token is never
logged, and a placeholder value is refused.
