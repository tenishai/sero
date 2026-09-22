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
- `ladon apply` converges instead of writing blind. Each operation is diffed
  against the live object and reported as create, update (naming the fields that
  differ), already current, or blocked; only the first two are written, so
  running it twice is safe. The dry-run now connects read-only to produce that
  diff, so it describes what would really change; `ladon plan` remains the
  offline render. Every write is read back and verified, so a change OpenBao
  accepts but does not hold fails loudly instead of appearing to succeed.
- A difference Ladon will not converge is reported as a blocked operation rather
  than an error that hides the rest of the plan. The whole plan is diffed before
  anything is written, so the report is always complete, and if any operation is
  blocked an apply writes nothing at all and exits non-zero. An engine type
  change and a KV version change are both blocked: tuning the version on a live
  v1 mount triggers OpenBao's one-way upgrade, which moves every secret under
  the mount, so it is an operator's decision and not a side effect of an apply.
- The diff shows values only for the fields Ladon itself sets and knows to be
  safe to print; every other field reports that it differs without showing
  either value, and credentials embedded in a URL are stripped from what is
  shown.
- The conventions lint also rejects cloud region literals, and the regions in
  the Terraform module docs, variables, and tests became placeholders or
  made-up values. It matches the GCP, AWS and Azure region shapes, including
  government and China partitions, and has its own tests.
- The GCP Secret Manager and AWS Secrets Manager stores take the same optional
  Kubernetes namespace restriction as the OpenBao store. It stays opt-in for
  those two: they are platform-level push targets with no team namespace to
  default to.
- Team onboarding takes the External Secrets Operator's service account name as
  a parameter, so a deployment that renames it can keep the role's bound subject
  and the store pointing at the same identity.
- Rendered Kubernetes documents can be applied to the cluster Ladon runs on
  (`ladon.install.k8s`, D18). They go as one stream through `kubectl apply
  --server-side --field-manager=ladon`, defaulting to a server dry-run, and the
  report names every document as created, configured, unchanged, or blocked.
  Ladon never passes `--force-conflicts`: a field another manager owns is
  reported as a blocked document, with the rest of the stream still applied and
  reported, so taking that field away stays an operator's decision. The same
  read-back-and-verify rule the OpenBao apply layer follows applies here, and
  the comparison covers only the fields the document sets, so server defaults
  and controller status do not read as drift. A mismatch names the kind, the
  object and the field path, never a value.
- An External Secrets Operator already running on the cluster is detected,
  reported and reused, and no helm command runs (D18a). The operator is
  cluster-scoped and singular, so installing a second one would leave two
  controllers fighting over one set of custom resource definitions. The install
  result reads `reusing external-secrets <version> in namespace <namespace>`;
  the version comes from the deployment's `app.kubernetes.io/version` label,
  falling back to its image tag. The pinned-chart install is unchanged for a
  cluster that has no operator. A failed lookup is an error rather than a
  "nothing found", because treating it as absence is how a second operator gets
  installed.
- The ESO custom resource API version can be probed from the cluster
  (`eso_api_version`), so resources are rendered at the version the cluster
  actually stores (`v1` or `v1beta1`) rather than at a version assumed from the
  chart. This is the API mismatch deployment-order.md warns about, now
  answerable at runtime. An absent CRD is an error, not an older version.

### Fixed

- JWT auth mounts pin `bound_issuer` to the discovery URL. Left empty, a mount
  accepted any token its discovery keys verified.
- JWT roles set `token_no_default_policy`, so a scoped workload role no longer
  also inherits the namespace's `default` policy, whose grants are outside
  Ladon's control.
- The per-team ESO role binds the operator's service account subject, not just
  the audience: any workload in a cluster can mint a token for a known audience.
- The rendered OpenBao `ClusterSecretStore` sets the OpenBao namespace it reads
  (without it, it read the root namespace rather than the team's KV) and carries
  conditions restricting which Kubernetes namespaces may reference it, matching
  on the API-server-set `kubernetes.io/metadata.name` label.
- The OIDC client-secret reference is written but never verified. It is Ladon's
  own reference, which OpenBao does not store, so comparing it made every
  read-back fail and every later dry-run report drift that was not there.
- Values that OpenBao echoes back in another shape (a composite duration, a
  boolean as a string, a one-element list as a bare value, a reordered nested
  claim) no longer read as drift, and a policy that exists but is empty is an
  update rather than a create.
- `deployment.kv_mount` must be a single path segment. A value with a separator
  in it would have enabled a mount other than the one the config named.
- Documentation drift: the baseline auth methods are JWT plus OIDC, with
  Kubernetes auth deliberately excluded, in the roadmap and source review as
  well as the baseline and design documents.

[Unreleased]: https://github.com/tenishai/ladon/commits/main
