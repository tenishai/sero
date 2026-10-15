<h1 align="center">Sero</h1>

<p align="center">
  <strong>Open-source secrets and vault management, built on OpenBao.</strong>
</p>

<p align="center">
  <a href="https://github.com/tenishai/sero/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/tenishai/sero/actions/workflows/ci.yml/badge.svg?branch=main"></a>
  <a href="LICENSE"><img alt="License: AGPL-3.0-or-later" src="https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg"></a>
  <img alt="Status: in development" src="https://img.shields.io/badge/status-in%20development-yellow.svg">
</p>

<p align="center">
  <a href="#overview">Overview</a> |
  <a href="#how-it-works">How it works</a> |
  <a href="#quickstart">Quickstart</a> |
  <a href="#stack">Stack</a> |
  <a href="#running">Running</a> |
  <a href="#status">Status</a> |
  <a href="#docs">Docs</a>
</p>

---

## Overview

Sero deploys and operates [OpenBao](https://github.com/openbao/openbao) (the
open-source fork of HashiCorp Vault) and syncs secrets out to where workloads
need them. It is a platform you can deploy: brand-neutral defaults, customizable
per deployment, open source under AGPL-3.0-or-later.

Sero integrates and operates OpenBao. It is not a fork of OpenBao or Vault. Its own
datastore holds no secret values (`src/sero/datastore/models.py`): values live in OpenBao
and in the targets External Secrets writes.

## How it works

- Provision and operate OpenBao across environments via pinned Helm installs,
  with cloud-KMS auto-unseal (GCP, AWS, or Azure) or Shamir.
- Render and apply configuration (namespaces, policies, JWT and OIDC auth, KV
  mounts) through the OpenBao API, dry-run first, from a `sero` CLI or a
  deployment config file.
- Sync secrets to targets with the [External Secrets Operator](https://github.com/external-secrets/external-secrets):
  Kubernetes clusters today. The GCP Secret Manager and AWS Secrets Manager stores are
  rendered (`src/sero/config/eso.py`) but not yet applied end to end (see Status).
- Give operators a web UI (secrets view, create/edit with a dry-run preview,
  namespace requests, admin) behind an identity-aware proxy, and agents an MCP
  tool surface (over stdio today; the OAuth 2.1 HTTP mount is on the roadmap).
- Keep secret values out of the UI and the datastore by default: reveal is explicit and
  audited (`src/sero/web/reveal.py`), and job errors pass through `redact_text`
  (`src/sero/hygiene.py`) before they are stored. Some raw exception messages still skip
  that redaction; closing them is on the list.

## Quickstart

Boot the backend on a laptop with SQLite and no OpenBao. You need
[uv](https://docs.astral.sh/uv/) (it fetches Python 3.14 for you, `.python-version`) and Node 22
for the UI.

```
git clone https://github.com/tenishai/sero && cd sero
uv sync --frozen
SERO_DATABASE_URL=sqlite:///./sero.db PYTHONPATH=src uv run python -m sero.web
curl localhost:8000/healthz     # {"ok":true}
```

In a second terminal:

```
cd ui && npm ci && npm run dev
```

Open <http://localhost:5173>; the dev server proxies `/api` to the backend. The process migrates
its datastore and serves, but with nothing else configured it holds no namespaces, knows no
identity (`/api/me` answers 401) and has no OpenBao to reach. The laptop section of
[docs/backend.md](docs/backend.md) adds the two things a real run needs, a dev identity
(`identity.allow_dev_identity` plus `SERO_DEV_IDENTITY`) and an OpenBao address and token, and
[deploy/openbao/README.md](deploy/openbao/README.md) installs a dev-shaped OpenBao on a cluster
with Helm.

Tests: `uv run pytest` for the backend and `cd ui && npm test` for the UI, the same gates CI runs.

## Stack

OpenBao on Integrated Storage (Raft) with cloud-KMS auto-unseal; a pinned Helm
install; configuration and runtime driven through the OpenBao API; the External
Secrets Operator for sync; a Python apply/agent backend on a SQLite datastore
(PostgreSQL with the `postgres` extra); and a Svelte 5 web UI that rebrands from two files.
Terraform provides only the OpenBao-specific substrate (the auto-unseal key);
creating the cluster is a separate concern (see
[Part of a larger platform](#part-of-a-larger-platform)).
The architecture and the decisions behind it are in
[docs/design.md](docs/design.md); every pinned version is in
[docs/versions.md](docs/versions.md).

## Running

The apply layers run from a checkout through the `sero` CLI wrapper. Every
command is a dry-run until `--apply` is passed, and every apply converges: a
second run reports each object already current and writes nothing
(`tests/test_apply_converge.py`).

```
# Put the External Secrets Operator on the cluster, or reuse the one already
# running on it:
scripts/sero install-eso --apply

# Onboard one application: its OpenBao policy and role, then its service
# account and secret store on the cluster, in one command:
scripts/sero apply --phase app-onboarding --config deployment.yaml \
  --team TEAM --app APP --cluster CLUSTER --k8s-namespace NAMESPACE --apply

# Sync one path into the Kubernetes Secret a workload already reads:
scripts/sero apply --phase sync --team TEAM --app APP \
  --path apps/APP/PURPOSE --name NAME --apply

# Check that nothing has been widened since: the JWT and OIDC roles, the
# default policy, and each per-application policy (src/sero/apply/audit.py).
# Read-only; exits 3 on a high finding, so a scheduled run can fail on it:
scripts/sero audit --config deployment.yaml
```

The OpenBao address and token come from the environment (`BAO_ADDR`/`VAULT_ADDR`
and `BAO_TOKEN`/`VAULT_TOKEN`). The full bring-up
sequence, and the rest of the phases, are in
[docs/deployment-order.md](docs/deployment-order.md).

## Status

Working, on main: apply that converges (dry-run first, diff shown, nothing destroyed) for both
OpenBao and cluster objects; teams as OpenBao namespaces with per-application identities; sync
through the External Secrets Operator; the HTTP backend behind an identity-aware proxy, on
SQLite (PostgreSQL with the `postgres` extra), with denied requests audited
(`src/sero/web/deps.py`, `src/sero/web/routes_secrets.py`); a machine lane for other
services; the role audit. Each piece was proven live on a shared development cluster when
it merged; [docs/status.md](docs/status.md) records what each proof covered.

Not yet: no OAuth 2.1 server / MCP mount over HTTP; no cloud secret-manager
sync; reveal step-up not built (gate denies by default); no fleet / second cluster; one process, one replica (the job runner and
the allow list are per process); no image build in CI, images are built by
hand from a clean export of a commit and tagged with its short sha.

The full list, with the PR for each piece and what the live proof covered, is in
[docs/status.md](docs/status.md); the phased plan is [ROADMAP.md](ROADMAP.md).

## Docs

- [docs/status.md](docs/status.md) - what is merged, by PR, and the live proof
- [CHANGELOG.md](CHANGELOG.md) - what shipped
- [ARCHITECTURE.md](ARCHITECTURE.md) - the repo card and its seams under docs/arch/
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

Sero is the secrets member of the tenish family of open-source platform and agent projects;
each is usable on its own. [gojira](https://github.com/tenishai/gojira) is the deploy platform,
with the same run-on-a-cluster-someone-else-created stance; [daedalus](https://github.com/tenishai/daedalus)
the agent kit and runtime; [cabal](https://github.com/tenishai/cabal) orchestration and the A2A
broker; [liminal](https://github.com/tenishai/liminal) the MCP gateway that fronts surfaces like
sero's; [techne](https://github.com/tenishai/techne) agent memory; [agents](https://github.com/tenishai/agents)
the fleet definitions. Sero's place: it holds the secrets. Operators reach it through the UI,
workloads through External Secrets, agents through its MCP tools, with the same hygiene rules
on every path.

## How this was built

tenishai is a small team of engineers who build with AI coding agents. Most of
this code was written in pairing sessions with Claude models in Claude Code: a
person set the design, directed the work and reviewed what landed, and the
agent wrote much of the code and docs. Many commits name the agent in a
`Co-Authored-By:` trailer, either a Claude model or the name of the
agent one of us pairs with. A commit without a trailer may still
have had AI help.

## License

[AGPL-3.0-or-later](LICENSE). Copyright (C) 2026 tenish and the sero contributors