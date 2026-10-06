# Changelog

All notable changes to Ladon are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

This changelog is for people. Entries explain what changed from a deployer's
point of view, not a list of commit subjects.

## [Unreleased]

### Changed

- CI runs `scripts/check_arch_cards.py`, so a seam changed without its card fails the PR
  (a stale anchor merged on 2026-10-05 because nothing ran it). Dependabot's uv and npm
  groups skip semver-major bumps; majors are a person's PR.
- **Docs say what ships.** The datastore is SQLite as shipped; PostgreSQL needs the new
  optional `postgres` extra (`psycopg[binary]`), which was never a dependency before. The
  machine-lane module docstring now says off-boarding is on the lane (`DELETE
  /api/service/app`), which PR #78 made true. (#80)

- **The README is a two-screen read.** The merged-by-PR status list and the live-proof
  account moved verbatim to `docs/status.md`; the sibling-project section is one paragraph.
  A live CI badge joins the header.

### Added

- `CODE_OF_CONDUCT.md` (Contributor Covenant 3.0) and Dependabot updates for the Python
  lock and the UI's npm packages, not only GitHub Actions.

### Changed

- Relicensed from Apache-2.0 to AGPL-3.0-or-later, matching the other tenishai
  projects. LICENSE, NOTICE, README and package metadata are updated.

### Added

- `DELETE /api/service/app` on the machine lane removes an application's
  identity: its ClusterSecretStore, its service account in Ladon's namespace,
  its JWT role and its read-only policy, the four objects `app-onboarding`
  (or a sync that onboarded implicitly) created. Cluster first, store before
  service account, then OpenBao, role before policy; every cluster object is
  read and checked for Ladon's field manager before it is deleted, and one
  that is not there is `absent`. Refused with a `409` while any ExternalSecret
  in the application's namespace still reads through the store. A dry run
  removes nothing. Every name is derived from the caller's pinned namespace
  and the application, so a caller can remove no identity but its own
  application's. The deploy manifests grant the two deletes this needs:
  `clustersecretstores` on the ClusterRole and `serviceaccounts` in Ladon's
  own namespace.

- The machine lane's `POST /api/service/sync` onboards the application when
  its `ClusterSecretStore` is not there. Until now the store, and the JWT role,
  read-only policy and service account behind it, existed only after an
  operator ran `app-onboarding` for that application. A consumer that creates
  applications on its own (an agent room minting one per agent) had no way to
  make that call, so its sync rendered an ExternalSecret pointing at a store
  nothing created: the Secret never arrived and the pod never started. The
  lane now reads the store first and, when it is absent, runs the same
  `app-onboarding` phase the CLI and the admin endpoint run, with the same
  renderers, for the caller's pinned OpenBao namespace and the Kubernetes
  namespace the sync names. A dry-run sync plans the onboarding and writes
  nothing. A blocked or failed onboarding is a `409` (or a `503` for a vault or
  cluster that would not answer) before the ExternalSecret is touched, so a
  document never points at an identity that did not land. The answer carries
  what onboarding did under `onboarding`, names and verbs only (and, for a
  blocked operation, why), and the audit row records `onboarded: true` for an
  applied onboarding or `would_onboard: true` for a planned one. Whose
  ExternalSecret an existing object is gets settled before any of this, so a
  refused sync has not created an identity on the way.

  A store that is already there admits exactly the Kubernetes namespaces
  onboarding named. A sync for the same application into a namespace the
  store does not admit is a `409` naming both, rather than an ExternalSecret
  ESO refuses at refresh time. Widening a store stays an operator's
  `app-onboarding` decision.

  This needs the deployment to name the cluster whose `jwt-<cluster>` mount the
  role hangs off: `deployment.cluster` in the config. A deployment that has not
  said refuses to onboard from the lane, with a message that names the key,
  rather than guess a mount.

  Off-boarding is unchanged: `DELETE /api/service/sync` still removes the
  ExternalSecret and only that. An application onboarded this way leaves its
  store, role, policy and service account behind when its consumer takes it
  down, exactly as an operator-onboarded one does.

### Fixed

- Every read of a live cluster object now passes `--show-managed-fields`.
  kubectl strips `metadata.managedFields` from `get -o json` unless asked, so
  the field-manager check behind the lane's ownership gates saw no manager on
  any live object, including the ExternalSecrets Ladon had applied itself:
  a second `POST /api/service/sync` over an application's own object, and
  every `DELETE /api/service/sync`, answered `409` with "managed by no
  recorded manager". The test double now drops managed fields the way kubectl
  does, so the gates are exercised against what the cluster really returns.

- The machine lane's `POST /api/service/sync` now has to prove whose
  ExternalSecret it is writing over, the same way the `DELETE` proves whose it
  is removing. `app` is a field in the request body and `k8s_namespace`
  defaults to it, and a server-side apply under Ladon's own field manager
  takes the fields it sets from Ladon's own earlier apply without a conflict,
  because as far as the API server can tell that is the manager doing its job.
  A caller pinned to one OpenBao namespace could therefore write over another
  team's live ExternalSecret, repoint it at a store it does control, and then
  pass the delete gate on an object it had just made look like its own. An
  object that is already there must read through this application's own
  `ClusterSecretStore` and pull a path inside `apps/<app>/`, or it is a `403`,
  and Ladon must be among its field managers, or it is a `409`.

  Two corrections to that check while it grew a second caller. It requires
  `secretStoreRef.kind` to be `ClusterSecretStore`, since ESO's default for a
  ref that names no kind is the namespaced `SecretStore` and a matching name in
  a ref of the other kind is a different object. And the refusal no longer
  echoes the live object's store name, which carries another tenant's team and
  application: what it names is derived from the request.

- Two namespaces pinned to one machine caller whose last path segments are
  equal are refused at config load. A store name is built from that segment, so
  `platform/team-a` and `other/team-a` name the same stores for the same
  application names, and the check above could not have told one team's object
  from the other's.

- A cluster delete tells a recreated object from a delete that did not happen.
  The read-back compared presence alone, so an object somebody created under
  the same name after the delete read as a failure. The uid recorded before the
  delete sorts three outcomes: `gone`, `terminating` when a finalizer has not
  run, and `recreated`. The route carries that through as `state`, with
  `recreated` spelled out beside it, since a caller that reads `deleted` and
  stops would be leaving a live ExternalSecret it does not know about. This
  narrows a window rather than closing it: `kubectl delete` takes no uid
  precondition.

- An ExternalSecret the delete route's own read found absent is answered there
  rather than handed to the removal, which reads again: an object created
  between the two would have been one nothing had checked.

- A create cannot overwrite a path that appeared while it ran. Every surface
  checks a path is absent before creating one, and the check and the write are
  two calls; the write now carries KV v2's `cas=0`, so the store refuses it and
  the caller gets a `409` instead of replacing somebody else's secret. The
  merge path has a window of the same shape and is not addressed here.

- The machine lane bounds `acting_email` at SMTP's 254 characters. The address
  pattern says nothing about length and the value reaches an audit row.

- Only a 404 from OpenBao means a secret path is not there. Both reads in
  the OpenBao wrapper swallowed every exception and reported the path as
  absent, so a denial, an expired token or an unreachable vault became
  "there is nothing there" and each surface above turned that into a
  confident wrong answer: a read answering `404` for a secret that exists, a
  delete reporting success over a path it never touched, and worst, an
  update merging into nothing and writing back over every key the path held.
  Anything that is not a 404 now surfaces as a `500` with an audit row.

- A cluster delete now checks ownership at the moment it deletes rather than
  in an earlier pass, passes `--wait=false` so one stuck finalizer cannot
  hold a request open, and reads the object back afterwards: kubectl
  accepting a delete is not the cluster having performed one. A manager entry
  scoped to a subresource no longer counts as ownership, since ESO records
  itself on the status of every ExternalSecret Ladon applies.

- The backend refuses to start on a secrets implementation that cannot serve
  every route it exposes, naming the missing methods, rather than answering
  a caller with a `500` the first time one is reached.

- Opening a namespace with no secrets in it answered "internal error": OpenBao
  reports an empty KV listing as a 404 and the reader let that escape. An empty
  namespace is now an empty list.
- The namespace listing stopped at the first folder, so a secret at
  `apps/<app>/<purpose>` showed only as an `apps/` entry with a count of zero.
  The listing now follows folders to the leaves (bounded in depth and count) and
  reports each secret by its full path.

### Proven live

The platform-vault program (PRs #57 to #68) took Ladon from complete-in-code
to running for real, on a shared development cluster, against a dedicated
OpenBao it operates. Proven, in one place:

- A single-node vault installed from the pinned chart.
- The full configuration rebuilt from config on a fresh vault; a second run
  wrote nothing, and a repointed store was updated in place rather than
  duplicated.
- A hand-edited policy showed up as a diff and was repaired by a re-apply.
- An existing External Secrets Operator on the cluster was detected and
  reused rather than fought over.
- The round trip from the vault through a store and an ExternalSecret to a
  Kubernetes Secret; the same ExternalSecret referenced from another
  namespace was refused.
- The acceptance matrix, with real projected tokens: an application's own
  prefix read, another application's prefix and the system paths and every
  write refused, a different service account refused with an invalid-subject
  error, and re-onboarding the same application a no-op.
- The role audit: clean on the platform vault, a deliberately widened
  default policy caught as a high finding, and clean again after the repair.
- The backend, deployed from the built image in the cluster, migrating the
  datastore at boot and serving the UI's routes through a port-forward.
- Application onboarding run as a background job through the admin endpoint,
  not only the CLI.

See the README's Status section for what this does and does not yet cover,
and [docs/plans/2026-09-22-platform-vault.md](docs/plans/2026-09-22-platform-vault.md)
for the task-by-task record.

### Added

- The machine lane can read and delete, so a caller that provisions an
  application can also take one down. Three routes, all authenticated,
  pinned, budgeted and audited exactly like the two that were already there.

  `GET /api/service/secrets` answers which keys a path holds, by name, with
  the version and when the current version was written. No value, masked or
  otherwise: the human read beside it returns a masked preview of every
  value, and a caller assembling an application needs to know whether
  `DB_URL` is there, not what it starts with. A path that is not there is a
  `404` and still an audited read.

  `DELETE /api/service/secrets` removes a path and every version of it, not
  KV v2's default soft delete, which leaves a path that still exists, still
  lists, and still holds its values. `DELETE /api/service/sync` removes the
  ExternalSecret in front of it, which otherwise stays behind and has ESO
  logging an error every refresh interval over a path that is gone on
  purpose. Both are dry run by default and name what they would remove; both
  are idempotent, because a destroy step that fails when it is run twice is a
  destroy step nobody can retry.

  A cluster object whose live field manager is not `ladon` is `409` naming
  the manager that owns it, and nothing is deleted. That is the mirror of
  never passing `--force-conflicts`: taking a field from another manager is
  an operator's decision, and so is deleting that manager's object. It must
  also be the application's own object: the live ExternalSecret has to read
  through that application's store and pull a path inside `apps/<app>/`, or
  it is a `403`. Without that, `app` being a field in the request body would
  let a caller pinned to one namespace reach another team's ExternalSecret.

  Removing an ExternalSecret removes the Kubernetes Secret with it, because
  Ladon renders them with `creationPolicy: Owner` and the Secret carries an
  owner reference back. That is the point of taking an application down, and
  it is the reason for the guards above: the blast radius is a running
  workload's Secret, not one custom resource. The answer says so in
  `removes_target_secret`, so a dry run tells a caller what an apply would
  take away. The deployment's ClusterRole grants `delete` on
  `externalsecrets` and no other kind.

  Scope, so nobody has to infer it: this removes one secret and one
  ExternalSecret. An application's ClusterSecretStore, JWT role, read-only
  policy and service account are off-boarding, and still an operator's job.

  A namespace listing says whether it is the whole namespace. The folder walk
  is bounded at a depth and a count, both of them silent until now, so a
  namespace with more secrets than the bound answered with a short list that
  looked exactly like a complete one. The listing carries `truncated`, and the
  UI says "More secrets than shown" under it.

  Reads have a budget of their own, `read_limit_per_minute`, 300 a minute
  against the write budget's 60. A caller watching an application come up
  polls the read, and one shared bucket would let that polling spend the
  allowance the same caller's writes need.

- A front door, so the backend is a service people can reach rather than a
  port-forward. `deploy/ladon/standalone/front-door/` holds four manifests
  for the cloud provider's external HTTPS load balancer with its
  identity-aware proxy: the backend settings (health check on `/healthz`,
  IAP on), a provider-managed TLS certificate, an http-to-https redirect,
  and the load balancer itself at a reserved global address.
  `standalone/service.yaml` gains the two annotations that bind them, and
  stays `ClusterIP`. Every deployment-specific value is a placeholder.

  Two things this cost to learn, now written down rather than rediscovered.
  The `ladon-backend` NetworkPolicy admitted ingress only from a namespace
  labelled `ladon.dev/proxy=true`, which admits nothing at all when the
  proxy is a cloud load balancer: traffic and health checks arrive from
  Google's `130.211.0.0/22` and `35.191.0.0/16`, direct to the pod, from no
  namespace. The policy now carries an `ipBlock` rule for the pair, and a
  cluster on an older copy of the manifests sees a healthy pod behind a
  permanently 502 load balancer until it reapplies. And IAP no longer takes
  per-application OAuth client credentials in its backend settings: the
  OAuth client administration API was shut down in 2026, so the client that
  every older guide tells you to create cannot be created, and IAP uses a
  Google-managed one instead.

  `docs/deployment-order.md` gains the ordered procedure (reserve the
  address, point DNS at it, apply, grant `roles/iap.httpsResourceAccessor`,
  then verify the assertion), including how to read the audience the backend
  has to match and a warning that setting `identity.assertion` closes the
  plain-header lane, so `/api` over a port-forward stops working from then
  on. `docs/backend.md` now shows the real proxy's values next to a generic
  one.

- A second ingress rule for a machine caller. A namespace labelled
  `ladon.dev/caller=true` reaches the backend on 8000, which is how a
  platform service using the machine lane (`POST /api/service/secrets`) gets
  in: it runs in a namespace of its own and matched neither the proxy rule
  nor a same-namespace selector. The label opens the port and grants no
  identity.

- Cloud-KMS auto-unseal you can install, not just describe. `scripts/ladon
  seal-values --config <deployment-config> --base-config <values file or HCL>`
  renders the Helm values overlay that carries the deployment's seal stanza in
  the OpenBao server config, with a repeatable
  `--service-account-annotation KEY=VALUE` for the binding that lets the vault
  pod reach the key as an identity the key is granted to. It prints the overlay
  and nothing else, and refuses a `shamir` deployment with exit 2. Until now
  the seal renderer had no consumer, and the base values file claimed an
  injection that never happened. `docs/deployment-order.md` gains the KMS path
  for a new cluster and a procedure for migrating a running Shamir vault in
  place.

- Two IAM grants on the GCP unseal key, not one: the `gcpckms` seal calls
  `cryptoKeys.get` at startup, so the unseal identity needs
  `roles/cloudkms.viewer` as well as
  `roles/cloudkms.cryptoKeyEncrypterDecrypter`. The Terraform module applies
  both with the key. Found live, as `error checking key existence`.

- The vault's NetworkPolicy admits the link-local metadata server on TCP 80,
  which is how workload identity hands the pod the credentials auto-unseal
  authenticates with. Found live: that address was allowed on port 53 alone,
  so the vault crash-looped behind the default deny with nothing in its logs
  about the network.

- DNS egress in the deploy manifests also admits the link-local resolver
  address, for clusters whose pods resolve through a node-local or cloud DNS
  address rather than the kube-dns pods. Proven live: with only the kube-dns
  pod selector, the vault could not resolve the workload token issuer and
  every store reported that it could not create a client; with the
  link-local rule the store validated and the sync recovered.

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
- HTTP backend (`ladon.web`, `docs/backend.md`): one process serving the UI's
  routes over the datastore and the vault, and the built UI beside them. A
  deployer needs to know four things. Identity is a header a trusted proxy sets
  (`identity.header`, default `x-goog-authenticated-user-email`); there is no
  login form, and a local hatch exists that warns at startup and is inert unless
  the config allows it. Who reaches which namespace comes from `access:` in the
  deployment config, by address and by group, with `admins` governing the admin
  routes; a directory-backed source can replace it without touching a route.
  Revealing a plaintext value is refused unless the deployment enables it.
  Migrations run at boot from `LADON_DATABASE_URL` (SQLite for development,
  PostgreSQL for anything shared) and a failed upgrade exits non-zero. Every
  route calls the same functions the agent surface calls, so the allow list, the
  namespace gate, placeholder refusal, the merge rules, and the audit row have
  one implementation; a consequence is that the allow list now gates the web
  surface too. Pins FastAPI and uvicorn.
- Background apply runs are recorded in a `jobs` table (kind, state, requester,
  timings, and the op-by-op report), ahead of the endpoints that drive them.
- Apply operations are endpoints that run as background jobs
  (`POST /api/admin/apply/{root-config|team-onboarding|app-onboarding|eso-install}`,
  admin only, documented in `docs/backend.md`). A phase takes longer than a load
  balancer will hold a request open, so the call answers `202` with a job id and
  `GET /api/admin/jobs/<id>` is what a caller polls: state, the elapsed time the
  server computes, and the op-by-op report, which names the object each
  operation touched and never a value, a policy body, or a token. The body is
  the flags the matching CLI command takes, and is a dry-run unless it says
  `{"apply": true}`; it cannot name a deployment config, because the config is
  the one the process was started with. One job of a kind runs at a time and a
  second request answers `409` with the running job's id. A job dials OpenBao on
  the deployment's own address and token rather than on anything a request
  carried. The ordering that makes an onboarding safe (both halves rendered
  before anything is written, OpenBao first, no cluster half after a blocked
  OpenBao half) is now one function the CLI and the endpoints share.
- Review fixes on the apply jobs: a restart now ends the jobs it interrupted
  (`failed`, `interrupted by a restart`) instead of leaving rows that claim a
  kind forever; the finish hook runs after the job's ending is recorded and in
  its own transaction, so a broken hook cannot roll a job back to `running`; an
  apply body naming another phase's fields, or a `server` carrying credentials
  in the URL, is a `400` rather than a job that fails later; a request that has
  already been decided cannot be decided again; approval records the decision
  before it starts the run, so the decision stands even when the run cannot
  start; the audit row is built from a named list of inputs and never records
  the OIDC client-secret reference; a dry-run operator install reports `would
  install` rather than claiming it installed; and credentials embedded in a URL
  are now stripped out of every string on its way into the audit trail, not
  only out of error messages.
- Approving a namespace request now applies that one team's namespace, as a
  `root-config` run narrowed to it (its namespace, the deployment's KV mount
  inside it, and the policies its access level grants; nothing at the root). The
  request records the job it started, and a run that does not land leaves its
  error on the row with the approval still standing. Approving a request for a
  team the deployment config does not define is refused: the namespace, the
  mount and the policies all come from that entry. The CLI takes the same
  narrowing as `--teams`.
- The agent surface records its denials and failures. The audit recorder moved
  to `ladon.audit`, which neither surface owns, and every MCP tool now runs
  through it, so a call refused before it starts (the allow list, the namespace
  gate) and a call that fails halfway leave the same rows they leave on the HTTP
  surface instead of nothing at all.

- Team namespaces are created from the deployment config. Root config now
  ensures `sys/namespaces/<namespace>` for every team in `teams:` and then,
  inside each namespace, enables the KV v2 mount the config names and writes the
  policies the team's `access:` level grants plus `external-secrets-read`. A
  namespace is only ever created, never deleted, and a second run reports it
  already current like every other operation. No config change is needed: the
  teams a deployment already lists are the namespaces it gets. The credential
  this step runs with now needs to be able to create namespaces and to write
  inside them, not just to configure the root.
- Per-application ESO identities (`ladon.config.apps`, and a new
  `app-onboarding` phase taking `--config`, `--team`, `--app` and `--cluster`,
  run through `scripts/ladon`):
  one application gets one path prefix (`kv/apps/<app>/...`), a policy
  `eso-<app>-readonly` granting read on that prefix and the four token self
  paths and nothing else, a JWT role `eso-<team>-<app>-<digest>` carrying only
  that policy and bound by subject to a service account created for that
  application alone, a `ServiceAccount` in Ladon's own namespace, and a
  `ClusterSecretStore ladon-<team>-<app>-<digest>` admitted into one Kubernetes
  namespace. A team namespace holds every application a team runs, so a single
  team-wide identity let one compromised workload read its neighbours' secrets.
  The names carry a short digest of the team and application pair because both
  parts may contain hyphens, which makes joining them with one ambiguous. The
  deployment config is required: the team's namespace and the KV mount are facts
  about the platform, and guessing either renders an identity that grants access
  to a path nothing uses. `--k8s-namespace`, `--sa-namespace` and `--audience`
  default sensibly; the plan prints the OpenBao operations and then the cluster
  documents as YAML. Applying the documents to a cluster is a later step; the
  command prints them and says so, and marks them as not to be applied when the
  OpenBao half did not land.
- The cluster half is applied by Ladon, and converges like the OpenBao half.
  `apply --phase app-onboarding --apply` now writes the OpenBao policy and role
  and then applies the service account and the store to the cluster, in one
  command; the cluster half is never applied when the OpenBao half did not land,
  because a store pointing at a role that does not exist fails later, at refresh
  time, somewhere else. Cluster documents now have the same `already current`
  no-op signal the OpenBao operations have: every document is read back and
  compared before anything is written, one that already matches never reaches
  `kubectl` at all, and a second run of an onboarding writes nothing and says so.
  What was written is read back again and verified, and a mismatch is an error
  naming the kind, the object and the field. Without `--apply` the same command
  sends the documents to the API server as a server-side dry run and reports
  what that cluster would really do, per document, prefixed `[cluster]`.
- The rendered store carries the External Secrets custom-resource version the
  target cluster stores, probed at apply time, rather than a fixed guess that
  the cluster may reject. `plan` stays offline and keeps rendering the current
  version; it is not allowed to dial a cluster.
- Applying an application onto a cluster with no External Secrets operator is
  refused before anything at all is written, naming `install-eso` as the fix:
  nothing would read the store that onboarding renders.
- `scripts/ladon install-eso [--apply] [--namespace NS]` puts the operator on
  the cluster, or reports and reuses the one already running on it and the
  custom-resource version it stores (D18a). The reuse path existed; nothing but
  a test could reach it.
- `apply --phase sync` renders and applies one `ExternalSecret` that carries a
  KV path into the Kubernetes Secret a workload already reads, through the store
  the application's onboarding created (`--team`, `--app`, `--path`,
  `--name`, plus `--k8s-namespace` when the namespace is not the application's
  own name, `--target-name` when the Secret's name differs, and
  `--refresh-interval` to change how often ESO re-reads the path from the `1h`
  default). The store is named through the same function that named it at
  onboarding, so the two cannot disagree, and a path outside the application's
  own prefix, or with a relative segment in it, is refused: its identity cannot
  read one, so the document would look right and be denied at refresh time, in
  the operator's logs, later. The dry-run names the path and the target Secret
  on the report line, so what is about to be wired is reviewable before it is.
- Verification of a store is exact where a subset comparison is not safe. A key
  added inside a store's `conditions` (a `namespaces` list beside the selector
  Ladon wrote) left the list one entry long and every field Ladon set unchanged,
  so it read as already current while the store had been opened to a namespace
  Ladon never admitted. Inside a store's conditions and a provider's auth block
  the live object may now hold nothing Ladon did not set, and anything else is
  reported as drift by its field path and re-applied. Everywhere else the subset
  rule stands, because a live object legitimately carries what the API server
  and its controllers put there.
- A container image for the backend (`Dockerfile`, `.dockerignore`). A
  multi-stage build: the Svelte UI is built with node and copied in as static
  files, the dependency set is exported from `uv.lock` and installed with
  `pip --require-hashes` so the image carries exactly the lock, and `helm` and
  `kubectl` are downloaded at their pinned versions and verified against the
  published sha256 for the architecture being built. Every base image is pinned
  by digest rather than tag, and the runtime stage upgrades its packages at
  build time: a base image is only as fresh as the day it was built, and a stale
  base ships every CVE patched since. The image runs as an unprivileged user
  (uid 10001), points `helm` and `kubectl` at a writable scratch path so it can
  run with a read-only root filesystem, and starts with `python -m ladon.web`,
  which migrates the datastore before it serves.
- An image workflow (`.github/workflows/image.yml`) that builds and pushes on a
  green `main`. It is triggered by the CI workflow completing, not by the push
  itself, so an image only ever exists for a commit whose checks passed. It
  authenticates through workload identity federation, holds no long-lived
  credential, and reads the registry, the identity provider, and the build
  service account from repository variables, so nothing deployment-specific
  appears in the repository. The tag is the short commit sha, and the pushed
  image's digest is printed and put in the job summary, since a tag can be moved
  and a digest cannot. The job runs only for a push from this repository, so a
  fork's run and a pull-request run (whose checks passed against merge-result
  code nobody merged) cannot reach its credentials.
- A deploy contract and manifests (`deploy/ladon/`). `gojira.yaml` describes the
  workload for a deployment platform whose generator emits the Deployment and
  Service; `k8s/base/` holds what that generator preserves (the service account,
  a namespaced Role over Secrets, ConfigMaps and ServiceAccounts, a ClusterRole
  limited to the ESO custom resources plus the namespace, CRD and Deployment
  reads the install layer needs, a 1Gi data volume, and default-deny network
  policies); `standalone/` holds a plain Deployment and Service for a cluster
  where that platform is not in use. The class is `gateway`, not `web`: Ladon is
  a single writer against a ReadWriteOnce volume, and a rolling update or a
  scale-out would wedge on the volume or run a background job twice.
  The Service is ClusterIP in both paths, and the network policy admits the
  backend only from a namespace labelled `ladon.dev/proxy=true`: there is no
  login form, the backend trusts an identity header a proxy sets, and anything
  that can reach it directly can claim any identity. A second label,
  `ladon.dev/eso=true`, admits the External Secrets Operator to OpenBao, which
  runs in its own namespace and so matches no same-namespace rule. OpenBao gets
  egress as well as ingress, because its JWT auth method calls out to the
  cluster's OIDC issuer to verify what a workload presented, and the backend
  gets outbound https for the proxy's JWKS and the chart repositories.
  `deploy/ladon/README.md` documents the bootstrap Secret's two keys, how the
  ConfigMap's key differs between the two paths, and why none of it is optional.
- `LADON_CONFIG_YAML`: the deployment config inline, in an environment
  variable. A ConfigMap consumed with `envFrom`, which is what the deploy
  contract's `config_from` is, turns every key into an environment variable, so
  a `config.yaml` key becomes a variable of that name and never becomes a file.
  Without this the backend on that path would have started with no config at
  all: nobody holding a namespace, nobody an admin, and nothing saying so.
  `LADON_CONFIG` still names a file and wins when both are set, because a
  deployment that names a file and does not mount it has a mistake worth
  failing on.
- `scripts/validate-deploy.sh`, wired into CI, checking both halves of the
  deploy directory without applying anything. With a reachable cluster it runs
  `kubectl apply --dry-run=client` for full schema validation; with none (CI) it
  proves the kustomization builds and the YAML parses, and says which mode it
  ran in, so a green run never overstates what it proved. Both modes assert the
  inventory: every object each half must produce, by kind and name, so a file
  dropped from the kustomization fails here rather than at apply time.
- An `image smoke` CI job building the image and running it once, with the
  flags a deployment uses (unprivileged, read-only root filesystem, writable
  scratch and data only), asserting `/healthz` answers 200. A Dockerfile can
  build cleanly and still produce something that cannot start.
- A "platform cluster, single node" path in `docs/deployment-order.md`: the
  short bring-up for one cluster operating one vault, from the namespace through
  the pinned chart, the manual initialize and unseal, the backend's own objects,
  the config and bootstrap Secret, the workload, and a port-forward for a first
  look. It says what it deliberately leaves out.
- `scripts/ladon audit [--config FILE] [--namespace NS] [--json]` reports every
  role and policy that grants more than it was meant to. For each namespace it
  reads every workload role on every JWT and OIDC auth mount (does it refuse the
  `default` policy, does it name `root`, does it pin a subject to one real
  service account, does it bind an audience, does every policy it names exist,
  and for a per-application role, does it carry only its own policy), the
  `default` policy's own body (does it reach `sys/policies/*`, `sys/auth/*` or
  `sys/mounts/*`, does it write or destroy any segment of the KV mount, does it
  glob a whole mount even at read, does it grant `sudo` anywhere), and every
  `eso-<app>-readonly` policy (does any grant leave the application's own
  prefix, reach another application's, or carry more than read and list). It
  connects read-only and writes nothing, so it needs no `--apply`. Without
  `--namespace` it covers the root plus every team namespace the deployment
  config names; with neither that nor `--config` it audits the root alone and
  says so on a `WARNING` line, because otherwise a clean report reads as a
  clean deployment. Human output is one line per finding,
  `[<namespace>] <severity> <code> <object>: <detail>`, and `--json` carries
  the same findings for a caller. Every report also carries the counts of what
  it looked at, and a namespace with no auth mount or no policies is reported
  rather than called clean. It exits 0 when every audited namespace is clean, 3
  when any has a high finding, and 2 when the audit could not run.

  This is the one deviation a scoped-identity model cannot survive and cannot
  see: the platform this baseline is drawn from had its `default` policy widened
  to full KV access plus `sudo` on the policy endpoints, so every role that
  inherited `default` had every capability and every per-application policy was
  decorative. Logins kept succeeding and nothing detected it for months.

  Policy bodies are read with a strict reader of the `path "<p>" { capabilities
  = [...] }` block form Ladon renders, tolerating comments and blank lines and
  adding no dependency. A body it cannot read becomes an `unparsed-policy`
  finding rather than a crash or, worse, a partial reading that silently vouches
  for the rest: high for `default` and for an application policy, which are the
  two the audit claims to judge, and medium for any other policy, which a
  deployment shapes for itself. An explicit `deny` is never a finding, because
  an audit that reports hardening teaches operators to stop hardening. No
  finding ever carries a policy body or a field value: it names the object, the
  path, and the capability.

  Ladon's own output is the fixture that keeps all of this honest. The test
  suite applies the real plans (root config, one team, three applications in
  one namespace) through the real executor and asserts the audit comes back
  with nothing to say. A rule that fires on what Ladon renders is not a rule
  about drift.

Proven live on a shared development cluster, against the platform vault:

- an External Secrets operator (v2.3.0) already running on the cluster was
  detected and reused, and its stored custom-resource version probed as `v1`;
- a first `app-onboarding --apply` created the application's service account and
  store; a second run was a complete no-op, reporting every OpenBao operation and
  every cluster document already current and issuing no apply at all;
- the store validated on the cluster;
- `sync` produced the Kubernetes Secret, carrying exactly the keys held at the
  path and no others;
- the negative: the same `ExternalSecret` applied from a namespace the store does
  not admit failed with `could not get secret data from provider`, and no Secret
  was created there.

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
- The `apply` command converges instead of writing blind. Each operation is
  diffed against the live object and reported as create, update (naming the
  fields that differ), already current, or blocked; only the first two are
  written, so running it twice is safe. The dry-run now connects read-only to
  produce that diff, so it describes what would really change; the `plan`
  command remains the offline render. Every write is read back and verified, so
  a change OpenBao accepts but does not hold fails loudly instead of appearing
  to succeed.
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
  report names every document as configured or blocked (server-side apply
  reports one verb for create and update); the read-back is what proves the
  state. Ladon never passes `--force-conflicts`: a field another manager owns is
  reported as a blocked document, with the rest of the stream still applied and
  reported, so taking that field away stays an operator's decision. A failure is
  classified per error line, so a denial that shares the output with a conflict,
  or an exit with no error line Ladon recognises, is an error rather than a
  quietly blocked document. The same read-back-and-verify rule the OpenBao apply
  layer follows applies here, and the comparison covers only the fields the
  document sets, so server defaults and controller status do not read as drift.
  A mismatch names the kind, the object and the field path, never a value. The
  "already current" signal the OpenBao half reports for a no-op is still owed
  for cluster documents; it comes with the CLI half, from the dry-run and the
  read-back rather than from kubectl's own verb.
- An External Secrets Operator already running on the cluster is detected,
  reported and reused, and no helm command runs (D18a). The operator is
  cluster-scoped and singular, so installing a second one would leave two
  controllers fighting over one set of custom resource definitions. The install
  result reads `reusing external-secrets <version> in namespace <namespace>`;
  the version comes from the deployment's `app.kubernetes.io/version` label,
  falling back to its image tag. The pinned-chart install is unchanged for a
  cluster that has no operator and no leftover custom resource definitions;
  definitions without an operator are a refusal, since installing over them
  would adopt somebody else's. A failed lookup, and more than one operator
  found, are both errors rather than a "nothing found", because treating either
  as absence is how a second operator gets installed.
- The ESO custom resource API version can be probed from the cluster
  (`eso_api_version`), so resources are rendered at the version the cluster
  actually stores (`v1` or `v1beta1`) rather than at a version assumed from the
  chart. This is the API mismatch deployment-order.md warns about, now
  answerable at runtime. An absent CRD is an error, not an older version.
- Every apply operation carries the OpenBao namespace it runs in, and the
  executor sets the namespace header for that operation's read, write and
  read-back before restoring it. One connection therefore serves a whole plan
  across many namespaces, and an operation is always diffed against the state in
  the namespace it would be written to: the same mount name in two namespaces is
  two objects, not one. Team onboarding now runs inside the team's namespace for
  this reason, and `--namespace` names the namespace the operations run in
  rather than scoping the connection. The rendered plan shows the namespace on
  each line, so two identical paths in two namespaces can be told apart.
- The backend can verify the proxy's signed assertion, and a deployment whose
  proxy can issue one should configure it (`identity.assertion`). A plain
  identity header is a claim that anything able to reach the process can make;
  an assertion is a proof. When configured it is required on every request and
  the plain header is not read. `docs/backend.md` states the matching deployment
  requirement: the pod must be reachable only through the proxy. Sending the
  deciding header twice is refused.
- A machine lane, so a service can write a secret on a person's behalf without
  holding a person's credential (`service_callers:` in the deployment config,
  `POST /api/service/secrets` and `POST /api/service/sync`). A caller presents an
  OIDC ID token, verified by exactly the machinery the human assertion uses, and
  everything that decides what it can do is pinned in config beside it: the
  issuer, an audience that must differ from the human assertion's (a config that
  repeats it is refused at load, so a browser credential cannot be replayed on
  this lane), the one subject the token may carry, the OpenBao namespaces it may
  write, and the Kubernetes namespaces it may place an object in. Tokens must
  also be fresh: this lane caps their age at 900 seconds from `iat`, whatever
  the issuer's own expiry says, and keeps usable signing keys for ten minutes
  rather than an hour once the key endpoint stops answering.
  `acting_email` is required on both routes and names the person the call is
  for. It is recorded and never authorizes: a caller may name any address,
  including somebody who has never used Ladon, so audit rows carry `lane`,
  `via` and `subject` alongside it to say which machine actually acted. The
  identity header and the development hatch are not read on these routes, and a
  deployment that names no callers refuses the lane outright. A caller may merge
  keys into one path under `apps/` (the human lane's merge rules, from the same
  functions) and converge one `ExternalSecret` against that application's own
  store, which is what makes the workload's Kubernetes Secret exist. That
  `ExternalSecret` lands in the application's own Kubernetes namespace unless
  the caller's `k8s_namespaces` admits another: the store's selector governs
  what ESO may read, and says nothing about where an object may be written, so
  where a cluster object lands is pinned here instead. A differing
  `target.name`, which is how an ExternalSecret adopts a Secret somebody else
  named, needs the same admission. A caller cannot read a secret, reveal a
  value, administer the deployment, write outside `apps/`, or touch a namespace
  it is not pinned to. Each caller has a call budget, 60 a minute by default,
  and every call including every denial leaves an audit row.
  One deployment requirement comes with it: admit a machine caller through the
  NetworkPolicy only where `identity.assertion` is configured. There is one port
  for both lanes, so on a header-only deployment an admitted workload could set
  the identity header and be any person on the human routes.
- One deployment serves one environment (`deployment.environment`). A call
  naming another is refused with 400 and the name of the one that is served, on
  the agent surface as well as over HTTP; `/api/me` reports it and the UI offers
  only that one. Every route accepted an environment and none of them used it,
  so two environments shared one set of paths under different labels.
- Updating a secret refuses two things it used to accept. A key named in both
  `values` and `remove_keys` is ambiguous, so it is refused rather than resolved
  by an order the caller cannot see. A merge that would leave a path with no
  keys is refused too: an empty secret still exists and reads as a path with
  nothing in it. Deleting a path stays a separate, explicit operation. Both
  apply to the agent surface and the web surface, which share the code.

### Fixed

- A migrated SQLite datastore assigns its own row ids. The first migration
  declared the generated ids as `BIGINT`, and SQLite only fills in a primary key
  it stores as the rowid alias, which is the exactly-`INTEGER` declaration. So
  on a database brought up by the migrations, every insert into `tool_call_log`
  and `namespace_requests` failed on the id it was supposed to be given: the
  audit write is one of those, so a request that reached it answered
  `internal error` and left no row to say so. A new revision restates both
  columns with the SQLite variant the models already carry (on PostgreSQL the
  type and its sequence are unchanged), and the tests now insert through every
  repository, and serve requests, against a migrated database rather than one
  built from the models.
- A failed or refused operation now leaves an audit row. It was written into the
  caller's session and rolled back with it, so the calls most worth
  investigating were exactly the ones that left no trace. Reads are covered as
  well as writes, and so is a request that never got past identity, which is the
  shape of somebody looking for a way in; those are attributed to
  `unidentified` and carry the deciding header's name, never its value. A
  refusal is recorded as `refused` and a denial as `denied`, so an operator can
  tell "we said no" from "we broke". Error messages are redacted before storage
  the way arguments already were.
- The signing-key URL for a proxy assertion must be https, and a key-endpoint
  outage no longer logs everybody out: the last key set that arrived keeps
  working for an hour, with refresh attempts rate limited, and past that window
  every assertion is refused. Thirty seconds of clock skew is tolerated.
- Only a route that means "not found" answers 404. A blanket handler turned any
  missing dict key into a 404 carrying the key name; everything else is now a
  500 that says only `internal error`.
- The UI reads the backend's error messages again. The client looked for a
  `detail` field and the backend sends `error`, so every explained failure
  reached the screen as "Forbidden". The UI also takes admin standing from
  `/api/me` instead of guessing it from namespace membership, which named the
  wrong people in both directions.
- An unexpected failure answers `internal error` and logs the traceback alone,
  rather than returning whatever the exception said.
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
