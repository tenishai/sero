# Changelog

All notable changes to Ladon are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

This changelog is for people. Entries explain what changed from a deployer's
point of view, not a list of commit subjects.

## [Unreleased]

### Added

- Repository foundations: Apache-2.0 license, NOTICE, README, secrets-hygiene
  `.gitignore`, `.gitattributes`, and `.editorconfig`.
- Project-local Vercel `web-design-guidelines` skill and `docs/skills.md`.
- `CONTRIBUTING.md` documenting the trunk-based development model, Conventional
  Commits, hard conventions, and the per-PR definition of done.
- Versioning policy (`docs/versioning.md`), this changelog, and the pinned
  versions reference (`docs/versions.md`).
- Conventions lint (`scripts/lint-conventions.sh`) enforcing no em dashes, no
  company-specific identifiers, no floating version pins, and no committed
  secret material.
- CI skeleton (`.github/workflows/ci.yml`) running the static layer on every
  pull request and push to `main`, with actions pinned by commit SHA.
- Architecture and decisions (`docs/design.md`) and the phased plan
  (`ROADMAP.md`): OpenBao on Integrated Storage (Raft) with KMS auto-unseal,
  agent-driven apply via Terraform plus Helm and the OpenBao SDK (no GitOps
  reconciler), ESO for sync to Kubernetes and GCP/AWS secret stores, Kubernetes
  and OIDC auth, a PostgreSQL backend datastore, an MCP tool surface, a custom
  Svelte 5 wrapper UI, and a polyglot Python plus TypeScript/Svelte stack. The
  decisions reflect a read-only review of the two source repositories.
- Source review (`docs/source-review.md`): the architecture of the two source
  repositories, the seams Ladon builds on, the scrub surface (as categories, not
  values), and the direction for the generic baseline.

[Unreleased]: https://github.com/kmgreiner3/ladon/commits/main
