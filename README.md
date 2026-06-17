<h1 align="center">Ladon</h1>

<p align="center">
  <strong>Open-source secrets and vault management, built on OpenBao.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <img alt="Status: foundations" src="https://img.shields.io/badge/status-foundations-orange.svg">
</p>

<p align="center">
  <a href="#overview">Overview</a> |
  <a href="#how-it-works">How it works</a> |
  <a href="#stack">Stack</a> |
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

- Provision and operate OpenBao across environments (dev, qa, prod) via pinned
  Helm installs.
- Sync secrets to targets with the [External Secrets Operator](https://github.com/external-secrets/external-secrets):
  Kubernetes clusters, GCP Secret Manager, and AWS Secrets Manager.
- Provide a secrets view and admin surface on top, with secrets-hygiene rules
  enforced throughout (no secret values are ever logged, printed, or committed).

## Stack

The concrete versions and the storage, auth, and apply-model decisions are being
worked out in the design phase. See [docs/design.md](docs/design.md) (in
progress) and the pinned versions in [docs/versions.md](docs/versions.md).

## Status

Foundations. No feature code yet. The repo is being bootstrapped: process docs,
architecture, conventions lint, and a CI skeleton land before any application
code. See [ROADMAP.md](ROADMAP.md).

## Docs

- [CONTRIBUTING.md](CONTRIBUTING.md) - how we work
- [docs/design.md](docs/design.md) - architecture and decisions
- [docs/versioning.md](docs/versioning.md) - SemVer, tags, changelog
- [docs/testing.md](docs/testing.md) - testing philosophy
- [docs/versions.md](docs/versions.md) - pinned versions
- [ROADMAP.md](ROADMAP.md) - phased plan

## Part of a larger platform

Ladon owns secrets and vault management within a broader SRE and agent
ecosystem. Sibling projects:

- **gojira** - deploy
- **argus** - observability
- **techne** - memory
- **limenal** - MCP gateway
- **cabal** - orchestration
