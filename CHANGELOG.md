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
  reconciler), ESO for sync to Kubernetes and GCP/AWS secret stores, JWT and
  OIDC auth, a PostgreSQL backend datastore, an MCP tool surface, a custom
  Svelte 5 wrapper UI, and a polyglot Python plus TypeScript/Svelte stack. The
  decisions reflect a read-only review of the two source repositories.
- Source review (`docs/source-review.md`): the architecture of the two source
  repositories, the seams Ladon builds on, the scrub surface (as categories, not
  values), and the direction for the generic baseline.
- Generic baseline and customization model (`docs/baseline.md`): a minimal
  platform root plus config-driven team namespaces (OpenBao open-source
  namespaces), generalized policy templates, baseline OIDC and JWT auth (AppRole
  optional; Kubernetes auth intentionally excluded), KV v2, and a three-part
  configuration model
  (deployment settings, teams, sync targets) with a minimal-deployment path.

- OpenBao install layer (`deploy/openbao/`): the pinned chart reference and
  opinionated, generic Helm values for HA on Integrated Storage (Raft) with the
  built-in UI, persistent data and audit volumes, and a per-deployment seal
  stanza, plus a dev overlay (single node, Shamir unseal) and a README
  describing the dry-run-first `helm upgrade --install` flow.

- Deployment config loader (`ladon.config.loader`, `config/example.yaml`): load
  a YAML deployment config into a `DeploymentConfig` (validated teams, plus
  deployment settings and sync targets), with a committed, brand-neutral example.
  Pins PyYAML.
- Teams config model (`ladon.config.teams`): parse and validate a deployment's
  team list (name, namespace, environments, identity-provider groups, access
  level) into `Team` objects, mapping each to its baseline policies. Rejects
  invalid namespaces and duplicate namespaces; an empty list is valid. Parses
  plain dicts, so it stays dependency-free. Tested.
- OpenBao policy rendering (`ladon.config.policies`): the baseline policy
  templates from baseline.md (`team-readwrite`, `team-readonly`,
  `external-secrets-read`) rendered as OpenBao HCL per namespace from the KV
  mount, with KV v2 data, metadata, and soft-delete paths. Pure rendering;
  applying to OpenBao is a later SDK step. Tested.
- OpenBao install tooling (`ladon.install`): a pinned `HelmRelease` and an
  `install()` that adds the pinned chart repo and runs `helm upgrade --install`,
  dry-run by default (an apply must be requested explicitly), per the apply model
  in design.md. Command construction is pure and unit-tested without a helm
  binary.
- Python project (`pyproject.toml`, `uv.lock`, `src/ladon/`, `tests/`): managed
  with uv, pinned to Python 3.14, with ruff (lint and format) and pytest. The
  first module is `ladon.hygiene` (refuse placeholder secret values, mask
  values), with tests. A `python lint and test` CI job runs ruff and pytest.
- CI contract check (`scripts/validate-helm.sh` and a `helm validate` CI job)
  that renders the pinned OpenBao chart with Ladon's base and dev values, so the
  install layer stays valid against the pinned chart. Pins `Azure/setup-helm`.
- Baseline auth rendering (`ladon.config.auth`): render the JWT auth method for
  in-cluster workloads and the OIDC method for human operators, with their roles,
  from parameters. Secrets are carried as references only (the OIDC client secret
  never passes through rendering); JWT verifies against the provider's published
  keys. Pure and tested.
- Auto-unseal seal rendering (`ladon.config.seal`): render the OpenBao seal
  stanza for a chosen provider, pluggable across clouds (GCP Cloud KMS, AWS KMS,
  Azure Key Vault) with Shamir as the no-cloud fallback. Key references only,
  never key material; an unknown provider is an error, not a silent fallback.
- SDK apply layer (`ladon.apply`): turn rendered policies, auth methods, and the
  KV mount into an ordered, value-free plan of OpenBao API writes (`plan`), and
  execute it through an authenticated `hvac` client (`executor`, `client`),
  dry-run by default with an explicit apply. Covers the phase-4 root config and
  phase-10 per-team ESO onboarding. Pins hvac.
- `ladon` command-line interface (`ladon.apply.cli`, `python -m ladon`,
  `scripts/ladon`): build and render a plan, or connect and apply it (dry-run
  unless `--apply`), for the root-config and team-onboarding phases. A `--config`
  deployment file supplies the KV mount and a team's namespace. Address and token
  come from the environment; placeholders are refused and nothing is logged.
- ESO resource rendering (`ladon.config.eso`): render `ClusterSecretStore`
  resources for the three providers (OpenBao over JWT, GCP Secret Manager over
  Workload Identity, AWS Secrets Manager over IRSA) and `ExternalSecret`
  resources, with the API group version parameterized. References and paths only,
  never values. Pure and tested.
- ESO install layer (`ladon.install.eso`): an agent-driven, dry-run-first Helm
  install of the External Secrets Operator, mirroring the OpenBao install, with
  explicit CRD ownership and a post-apply webhook health check. Pins the ESO
  chart.
- Terraform substrate (`terraform/`): the OpenBao-specific cloud substrate,
  scoped to what runs on an existing cluster. The GCP unseal-key module
  provisions the KMS keyring, key, and key-use IAM grant as one unit, via the
  official registry KMS module. Cluster, network, and project provisioning are
  out of scope (a separate foundation repo owns those). A `terraform validate`
  CI job runs fmt and validate; pins Terraform, the Google provider, the KMS
  module, and `hashicorp/setup-terraform`.

### Changed

- Apply model refined into three separated, agent-driven layers, each with its
  own dry-run: Terraform for the cloud substrate only (D10), a direct Helm
  install of OpenBao instead of the Terraform Helm provider (D3a), and the
  OpenBao SDK/API for configuration instead of the Terraform Vault provider
  (D3b). This decouples the install and configuration lifecycles from
  infrastructure and avoids configuring Terraform providers from a cluster that
  does not exist at plan time.
- ESO installation defined as one agent-driven Helm bootstrap capability (D4a),
  triggered for the hub cluster at bring-up and per target cluster from the
  UI/MCP, rather than a static deploy step. Target clusters are registered over
  time, so ESO is installed on demand once networking and auth are in place.

[Unreleased]: https://github.com/tenishai/ladon/commits/main
