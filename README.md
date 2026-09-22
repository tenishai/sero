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
Secrets Operator for sync; a Python apply/agent backend with a PostgreSQL
datastore; and a Svelte 5 web UI that rebrands from two files. Terraform provides
only the OpenBao-specific substrate (the auto-unseal key); creating the cluster
is a separate concern (see [Part of a larger platform](#part-of-a-larger-platform)).
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

In development, and feature-complete end to end in code. Implemented and tested:

- the OpenBao Helm install and auto-unseal seal rendering (GCP, AWS, Azure, or
  Shamir), plus the Terraform module for the auto-unseal key;
- the configuration and apply layer (namespaces, policies, teams, JWT and OIDC
  auth, KV mounts) as a dry-run-first plan/apply engine with a `ladon` CLI;
- the External Secrets Operator install and `ClusterSecretStore` /
  `ExternalSecret` rendering for Kubernetes, GCP Secret Manager, and AWS
  Secrets Manager;
- the PostgreSQL datastore (audit, token families, allow list, namespace-request
  workflow, reveal grants);
- the MCP agent surface (read and dry-run-first write tools, OAuth 2.1 bearer
  auth, allow-list gating);
- the HTTP backend (FastAPI): the UI's routes over the datastore and the vault,
  identity from a trusted proxy header, namespace membership from the deployment
  config, migrations at boot, and the built UI served from one process. See
  [docs/backend.md](docs/backend.md);
- the Svelte 5 web UI (secrets view with on-demand reveal, create/edit with a
  dry-run preview, namespace requests, an admin panel), rebrandable from two
  files;
- the container image and deploy contract: a digest-pinned multi-stage build
  carrying the built UI and checksum-verified `helm` and `kubectl`, an image
  workflow that pushes on a green `main` through workload identity federation,
  and Kubernetes manifests (identity, least-privilege RBAC, data volume, and
  default-deny network policies) for a generated deployment or a plain one
  (`deploy/ladon/`).

Everything above runs and is tested offline (unit tests plus a browser render
smoke test); the one milestone still ahead is a live end-to-end run against a
real cluster, OpenBao, and PostgreSQL. See [ROADMAP.md](ROADMAP.md).

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
