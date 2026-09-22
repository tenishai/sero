<h1 align="center">Ladon</h1>

<p align="center">
  <strong>Open-source secrets and vault management, built on OpenBao.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <img alt="Status: in development" src="https://img.shields.io/badge/status-in%20development-yellow.svg">
</p>

<p align="center">
  <a href="#overview">Overview</a> |
  <a href="#how-it-works">How it works</a> |
  <a href="#stack">Stack</a> |
  <a href="#running">Running</a> |
  <a href="#status">Status</a> |
  <a href="#docs">Docs</a>
</p>

---

## Overview

Ladon deploys and operates [OpenBao](https://github.com/openbao/openbao) (the
open-source fork of HashiCorp Vault) and syncs secrets out to where workloads
need them. It is a platform you can deploy: brand-neutral defaults, customizable
per deployment, 100% open source under Apache-2.0.

Ladon integrates and operates OpenBao. It is not a fork of OpenBao or Vault, and
it never stores secret values.

## How it works

- Provision and operate OpenBao across environments via pinned Helm installs,
  with cloud-KMS auto-unseal (GCP, AWS, or Azure) or Shamir.
- Render and apply configuration (namespaces, policies, JWT and OIDC auth, KV
  mounts) through the OpenBao API, dry-run first, from a `ladon` CLI or a
  deployment config file.
- Sync secrets to targets with the [External Secrets Operator](https://github.com/external-secrets/external-secrets):
  Kubernetes clusters, GCP Secret Manager, and AWS Secrets Manager.
- Give operators a web UI (secrets view, create/edit with a dry-run preview,
  namespace requests, admin) and agents an MCP tool surface, both behind OIDC.
- Enforce secrets hygiene throughout: no secret value is ever logged, printed,
  committed, or shown by default; reveal is explicit and audited.

## Stack

OpenBao on Integrated Storage (Raft) with cloud-KMS auto-unseal; a pinned Helm
install; configuration and runtime driven through the OpenBao API; the External
Secrets Operator for sync; a Python apply/agent backend with a SQLite or
PostgreSQL datastore; and a Svelte 5 web UI that rebrands from two files.
Terraform provides only the OpenBao-specific substrate (the auto-unseal key);
creating the cluster is a separate concern (see
[Part of a larger platform](#part-of-a-larger-platform)).
The architecture and the decisions behind it are in
[docs/design.md](docs/design.md); every pinned version is in
[docs/versions.md](docs/versions.md).

## Running

The apply layers run from a checkout through the `ladon` CLI wrapper. Every
command is a dry-run until `--apply` is passed, and every apply converges: a
second run reports each object already current and writes nothing.

```
# Put the External Secrets Operator on the cluster, or reuse the one already
# running on it:
scripts/ladon install-eso --apply

# Onboard one application: its OpenBao policy and role, then its service
# account and secret store on the cluster, in one command:
scripts/ladon apply --phase app-onboarding --config deployment.yaml \
  --team TEAM --app APP --cluster CLUSTER --k8s-namespace NAMESPACE --apply

# Sync one path into the Kubernetes Secret a workload already reads:
scripts/ladon apply --phase sync --team TEAM --app APP \
  --path apps/APP/PURPOSE --name NAME --apply

# Check that nothing has been widened since: every JWT role, the default
# policy, and every per-application policy. Read-only; exits 3 on a high
# finding, so a scheduled run can fail on it:
scripts/ladon audit --config deployment.yaml
```

The OpenBao address and token come from the environment (`BAO_ADDR`/`VAULT_ADDR`
and `BAO_TOKEN`/`VAULT_TOKEN`); the token is never logged. The full bring-up
sequence, and the rest of the phases, are in
[docs/deployment-order.md](docs/deployment-order.md).

## Status

All merged on main:

- Apply converges (create / update with a shown diff / already current), reads
  back every write, blocks instead of destroying; `plan` is offline, `apply`
  without `--apply` connects and diffs. (PR #59)
- Teams in config become OpenBao namespaces with the KV mount and team
  policies inside; team onboarding runs inside the namespace; per-application
  identities (`app-onboarding`): a read-only policy on the app's prefix, a JWT
  role pinned to a dedicated service account with no default policy, a
  ClusterSecretStore scoped to the OpenBao namespace and to the app's
  Kubernetes namespace by its immutable name label. (PR #61)
- Cluster documents applied with server-side apply under the `ladon` field
  manager, read back and verified with strict comparison under
  `spec.conditions` and the auth block, with an already-current signal; an
  operator already on the cluster is detected and reused; `install-eso`;
  `--phase sync` for one ExternalSecret. (PRs #60, #63)
- The HTTP backend: proxy-header or signed-assertion identity, config group
  source, the UI's routes, one environment per deployment, audit rows in
  their own session including denials and 401s, reveal behind a
  deny-by-default gate, migrations at boot, SQLite or PostgreSQL. (PR #62)
- Image (pinned by digest, helm/kubectl by checksum, non-root, read-only
  rootfs), image workflow behind repository variables, deploy contract for
  the deploy platform (class gateway), standalone manifests, three
  NetworkPolicies including the link-local DNS rule, a CI image smoke. (PRs
  #64, #67)
- The role audit (`scripts/ladon audit`). (PR #65)
- The machine lane: `POST /api/service/secrets` and `/api/service/sync` per
  configured OIDC caller, pinned subject and namespaces, attribution via
  `acting_email`. (PR #66)
- The machine lane reads and deletes: `GET /api/service/secrets` for the key
  names, the version and when a path last changed; `DELETE` on both routes,
  removing a path with all of its versions and the ExternalSecret in front of
  it. Dry run by default, idempotent, and a cluster object another field
  manager owns is `409` rather than removed. The lane can now delete; the
  room's destroy step is the remaining half.
- Apply phases as admin jobs with a restart sweep; namespace-request approval
  applies the team; MCP denial audit; the SQLite primary-key migration. (PR
  #68)

Proven live on a shared development cluster: a single-node vault installed
from the pinned chart; the full configuration rebuilt from config on a fresh
vault, with a second run writing nothing and a repointed store updated in
place; a hand-edited policy diffed and repaired; an existing External Secrets
Operator detected and reused; the round-trip vault -> store -> ExternalSecret
-> Secret, and the same ExternalSecret from another namespace refused; the
acceptance matrix with real projected tokens (own prefix readable, another
app's prefix and system paths and writes refused, a different service
account refused with invalid subject, re-onboarding a no-op); the audit
clean, then a widened default policy caught as HIGH, then clean after
repair; the backend deployed from the image in the cluster, migrating at
boot and serving the UI's routes through a port-forward; app onboarding run
as a job through the admin endpoint. The platform vault runs on cloud-KMS
auto-unseal, migrated in place from Shamir on the running vault and proven by
a pod delete it came back from unsealed on its own. The backend now sits
behind an identity-aware proxy on that cluster: a cloud external HTTPS load
balancer terminating a managed certificate at a reserved address, with the
proxy authenticating every caller and the backend verifying the signed
assertion rather than trusting a header.

Not yet: no OAuth 2.1 server / MCP mount over HTTP; no cloud secret-manager
sync; reveal step-up not built (gate denies by default); no application
off-boarding phase (the lane removes a secret and its ExternalSecret; the
store, the role, the policy and the service account are still an operator's
job); no fleet / second cluster; one process, one replica (the job runner and
the allow list are per process); the image workflow publishes only once the
deployer grants its identity.

See [ROADMAP.md](ROADMAP.md) for the phased plan.

## Docs

- [CONTRIBUTING.md](CONTRIBUTING.md) - how we work
- [docs/design.md](docs/design.md) - architecture and decisions
- [docs/versioning.md](docs/versioning.md) - SemVer, tags, changelog
- [docs/testing.md](docs/testing.md) - testing philosophy
- [docs/versions.md](docs/versions.md) - pinned versions
- [docs/deployment-order.md](docs/deployment-order.md) - bring-up sequence and the apply flow
- [docs/backend.md](docs/backend.md) - the HTTP backend: running it, its environment, identity
- [ui/README.md](ui/README.md) - the web UI and how to rebrand it
- [ROADMAP.md](ROADMAP.md) - phased plan

## Part of a larger platform

Ladon is the secrets and vault member of the **tenish family** of open-source
platform and agent projects. Each is independent and usable on its own; together
they cover infrastructure, secrets, and the agent runtime. How Ladon relates to
the others:

**Infrastructure and platform**

- [**gojira**](https://github.com/tenishai/gojira) - an opinionated GKE deploy
  platform. It registers an existing cluster and provisions per-app deploy
  plumbing (service account, workload identity, CI, manifests). Ladon takes the
  same "run on a cluster someone else created" stance; a separate foundation
  layer owns cluster, network, and project creation, and both Ladon and gojira
  run on top of it.

**Agent runtime** (Ladon exposes an MCP tool surface into this world)

- [**daedalus**](https://github.com/tenishai/daedalus) - an agent construction
  kit: an agent is a directory (persona, agent card, wiring), and daedalus is the
  runtime, with Protocol seams for memory, tools, and checkpoints.
- [**kaminos**](https://github.com/tenishai/kaminos) - a visual interface builder
  for daedalus agent bundles.
- [**cabal**](https://github.com/tenishai/cabal) - a self-hosted agent
  orchestration platform and A2A broker that runs personas as modules.
- [**liminal**](https://github.com/tenishai/liminal) - an MCP/tool gateway: one
  connection per agent, many backend handlers behind it. Ladon's MCP surface is
  exactly the kind of backend a gateway like this fronts.
- [**techne**](https://github.com/tenishai/techne) - local-first agentic memory
  (a single SQLite file), the memory seam daedalus agents plug into.
- [**agents**](https://github.com/tenishai/agents) and
  [**prudence-memory**](https://github.com/tenishai/prudence-memory) - portable
  definitions of the agent fleet (Prudence the reviewer, and others) and their
  shared team-memory corpus.

Ladon's place in this picture: it holds the secrets. Human operators use its UI
(OIDC); workloads read secrets through the External Secrets Operator; and agents
built on daedalus/cabal reach it through its MCP tools (behind liminal), with the
same secrets-hygiene rules applied to every path.
