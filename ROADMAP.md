# Roadmap

This is the phased plan for Ladon at a glance. The architecture and the
decisions behind it are in [docs/design.md](docs/design.md). The plan is
deliberately incremental: each phase is releasable and builds on the last.
Foundations first, quality over velocity.

## Phase 0: Foundations

Bootstrap the repository with good hygiene before any feature code: license,
README, conventions, process docs, the conventions lint, and a CI skeleton, plus
the architecture in [docs/design.md](docs/design.md) and this roadmap.

- [x] Repo skeleton: license, README, ignores, editor config
- [x] Project-local skills committed
- [x] CONTRIBUTING.md
- [x] Versioning policy, changelog, pinned-versions reference
- [x] Testing philosophy
- [x] Conventions lint and CI skeleton
- [x] Architecture (docs/design.md) and this roadmap

## Phase 1: Understand the sources, design the baseline

Read the two existing source repos read-only, document their architecture, and
design how Ladon pulls the useful parts in and rebuilds them generically. Two
mandates apply to everything brought over: scrub all company branding, and
generalize and slim down to a small handful of generic namespaces, policies, and
auth methods driven by configuration.

- [x] Document each source repo's architecture ([docs/source-review.md](docs/source-review.md))
- [ ] Design the generic baseline (namespaces, policies, auth methods)
- [ ] Design the per-deployment customization model
- [ ] No feature code until the baseline design is reviewed

## Phase 2: OpenBao deploy baseline (dev)

Stand up OpenBao on a single environment with the chosen architecture.

- Pinned Helm install of OpenBao on Integrated Storage (Raft), HA
- The generic baseline: namespaces, policies, KV engines
- Auth methods: Kubernetes (workloads) and OIDC (operators)
- The direct apply tooling with dry-run by default
- cert-manager for TLS, confirmed and pinned

## Phase 3: Secret sync with ESO

Sync secrets out to the supported targets.

- Pinned install of the External Secrets Operator
- `SecretStore` and `ExternalSecret` rendering for Kubernetes, GCP Secret
  Manager, and AWS Secrets Manager
- The gated integration smoke test: a dummy secret round-trips from OpenBao
  through ESO to a target and back
- Terraform for the cloud-side resources (IAM, store access)

## Phase 4: Secrets view and admin UI

The custom wrapper application.

- Secrets view: namespaces, paths, key names (never values), sync status
- Admin surface: manage namespaces, policies, and sync targets
- OIDC login, reviewed against the web-design-guidelines skill
- Never displays secret values

## Phase 5: Agent surface and multi-environment hardening

- Expose selected OpenBao operations as MCP tools, with secrets-hygiene behavior
  baked in (dry-run, names only, refuse placeholders)
- Extend to qa and prod environments
- Backup/restore runbooks, upgrade procedures, and operational hardening

## Principles that hold across every phase

- Never store, log, or display secret values.
- Brand-neutral: nothing company-specific; everything deployment-specific is a
  parameter.
- Pin everything; bumps are their own reviewed PRs.
- Small, single-purpose changes; build, review, revise; keep `main` releasable.
