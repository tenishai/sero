# Sero design

This document describes Sero's architecture and the decisions behind it. It is
the reference for why the system is shaped the way it is. The phased plan that
follows from it lives in [../ROADMAP.md](../ROADMAP.md).

Sero is an open-source secrets and vault management platform built on OpenBao.
It deploys and operates OpenBao, syncs secrets out to clusters and cloud secret
stores through the External Secrets Operator, and provides a secrets view and
admin surface on top. It is brand-neutral and customizable per deployment.

## Scope

In scope:

- Provision and operate OpenBao across environments (dev, qa, prod).
- Manage the Helm installs for everything OpenBao-related.
- Run ESO to sync secrets to Kubernetes clusters, GCP Secret Manager, and AWS
  Secrets Manager.
- A secrets view and admin surface, including ESO and sync status.
- A small, generic baseline of namespaces, policies, and auth methods that a
  deployer customizes through configuration.

Out of scope:

- Forking OpenBao or the External Secrets Operator. Sero integrates and
  operates them; it does not modify or redistribute them.
- Storing secret values anywhere in this repository.
- Company-specific configuration. Anything deployment-specific is a parameter,
  not a hardcoded value.

## Decisions

Each decision records the choice and the reason. Versions are pinned in
[versions.md](versions.md); this table records direction, not pins.

| # | Decision | Choice | Why |
| --- | --- | --- | --- |
| D1 | Core vault | OpenBao | Open-source, community-driven, Apache-2.0, the OSS path for Vault-compatible secrets management. |
| D2 | Storage backend | Integrated Storage (Raft) | The recommended production backend. No external dependency, built-in HA, and snapshot backup/restore. Keeps Sero easy to deploy: nothing to provision but OpenBao itself. |
| D2a | Seal / unseal | Auto-unseal via a cloud KMS (pluggable) | Auto-unseal removes manual key-share handling and is the practical default for HA. The KMS provider (GCP Cloud KMS, AWS KMS, others) is parameterized per deployment. Manual/Shamir unseal remains supported for deployments without a cloud KMS. |
| D3 | Apply model | Agent-driven, three layers (Terraform, then Helm, then SDK) | Sero's agents deploy and maintain state themselves, in three separated layers, each with its own dry-run: (1) Terraform for the cloud substrate (D10); (2) a direct Helm install of OpenBao (D3a); (3) the OpenBao SDK/API for configuration and runtime (D3b). The layers have independent lifecycles and pass outputs forward (substrate outputs feed the install; install feeds config). Every write defaults to dry-run with a diff against live state. There is no GitOps reconciler; the agent owns the plan, the diff, and the apply at each layer. GitOps is recorded as a considered, rejected alternative below. |
| D3a | OpenBao install | Direct Helm (`helm upgrade --install`), not the Terraform Helm provider | Installing OpenBao through `helm_release` couples an in-cluster app lifecycle to the infrastructure apply and hits the Terraform anti-pattern of configuring a provider from a cluster that does not exist at plan time. A direct Helm step (pinned chart, values templated from Terraform outputs) lets OpenBao be installed, upgraded, and rolled back independently of the substrate, with `helm diff` as the dry-run. |
| D3b | OpenBao configuration | OpenBao SDK/API (agent-driven), not the Terraform Vault provider | Namespaces, policies, auth methods, KV mounts, and ESO resources are rendered from config and applied through the SDK/API with a diff first. This keeps configuration (which changes often) decoupled from infrastructure state, avoids the provider-from-data-source ordering problem, and matches how the source backend already manages this. |
| D4 | Secret sync | External Secrets Operator | The de facto standard for syncing secrets into Kubernetes and between secret stores. Broad provider support including GCP Secret Manager and AWS Secrets Manager. |
| D4a | ESO install | One agent-driven Helm bootstrap capability, triggered for the hub cluster at platform bring-up and per target cluster from the UI/MCP | Target clusters are registered over time and cannot be pre-provisioned at deploy time, so ESO is installed on demand (pinned chart, CRD-ownership handling, webhook health check), generalized from the source backend's bootstrap. The same capability installs ESO on the hub cluster during bring-up (replacing the source's GitOps path, since Sero has no reconciler). It is the D3a Helm layer applied per cluster, so it shares one mechanism and one `helm diff` dry-run. It depends on network reachability to OpenBao and an ESO auth role (D6), so it sequences after the deploy baseline, the backend, and networking. |
| D5 | Sync targets | Kubernetes, GCP Secret Manager, AWS Secrets Manager | The three targets the platform must serve. Each is an ESO `SecretStore` / `ClusterSecretStore` provider, parameterized per deployment. |
| D6 | Auth methods | Baseline JWT and OIDC; AppRole available | JWT for in-cluster workloads and OIDC for human operators are the generic, provider-agnostic baseline. ESO authenticates with a JWT role (a projected service account token verified against the cluster's published keys, so no secret is stored), with a JWT mount per cluster covering the cross-cluster case. AppRole is available for service-to-service automation without a workload identity. The source deployment uses several methods and is migrating workloads off Kubernetes auth to JWT; Sero starts where that migration lands, so Kubernetes auth is not in the baseline. |
| D7 | UI | Custom wrapper application, Svelte 5 | A separate UI that spans multiple clusters, ESO sync status, and the cloud secret stores. OpenBao's built-in UI is single-cluster and CRUD-focused and cannot show the cross-cluster and cloud-sync views that are Sero's reason to exist. Svelte 5 is chosen to reuse and generalize the existing source UI rather than rebuild it; it is a strong fit for an admin/status surface. |
| D8 | Languages | Polyglot: Python and TypeScript/Svelte | Python for the backend, operational tooling, and the agent/MCP surface (consistent with the sibling repos and a strong fit for Kubernetes and cloud SDKs). TypeScript/Svelte for the wrapper UI (D7). |
| D9 | TLS / certificates | cert-manager (candidate) | For issuing and rotating OpenBao and ingress certificates. Confirmed and pinned when the deploy baseline lands. |
| D10 | Infrastructure as code | Terraform with pinned providers, scoped to the OpenBao-specific substrate | Terraform here owns only the cloud resources specific to running OpenBao on a cluster that already exists: the KMS key for auto-unseal and its IAM grant, and (later) OpenBao node pools and snapshot storage. It does not create the cluster, the network and CIDR plan, or the projects/accounts: those belong to a separate foundation (landing-zone) repo that stands up accounts and clusters to best practices, the way an org platform repo does. Sero, like a deploy platform that registers an existing cluster, runs on top of what the foundation provides. Terraform does not install OpenBao (D3a) or configure it (D3b); it produces outputs those layers consume. Providers and registry modules pinned in versions.md; prefer official cloud-provider registry modules over hand-rolled resources. |
| D11 | Backend datastore | PostgreSQL | The backend needs durable state that is not secret material: audit log, MCP token families, step-up reveal grants, and namespace-request workflow. None of it is secret values. PostgreSQL is the conventional, portable choice. |
| D12 | Agent / MCP surface | MCP server exposing selected operations | Selected OpenBao operations are exposed as MCP tools (OAuth 2.1), with secrets-hygiene behavior baked in (dry-run, names only, refuse placeholders). This is how Sero fits the agent ecosystem; built with the mcp-builder skill. |

### Decisions from the platform vault stage (2026-09-22)

Recorded as they were decided, when the project was still named Ladon, so the names in
these rows are the names of that day. [ARCHITECTURE.md](../ARCHITECTURE.md) has the names
the code uses now.

| # | Decision | Choice | Why |
| --- | --- | --- | --- |
| D13 | Which OpenBao | Ladon owns a dedicated OpenBao for the platform it serves. It never configures an OpenBao another tool manages. | The plans here write mounts, roles and policies by fixed names. Two writers converging the same names with no shared ownership model silently undo each other. A dedicated instance turns every gap in this repo into a bug found on its own vault, not an outage on someone else's. |
| D13a | Dev shape | Single node, Raft, Shamir unseal, one small pod, in namespace `ladon` on the platform cluster. KMS auto-unseal and HA come when an environment needs them, by configuration. | Lighter weight is the brief. Nothing in the consumer contract depends on HA. |
| D14 | The service | One long-running process (FastAPI): the backend API, the built UI, and later the MCP surface, deployed as a `web` workload. Every apply operation (root config, team onboarding, app onboarding, ESO install) is an authenticated endpoint that runs as a tracked background job. | The deploy platform has no one-shot job class, apply operations exceed a load balancer's timeout, and an endpoint gives every operation an audit row for free. The CLI stays for operators and tests. |
| D15 | Human identity | A trusted proxy header (name configurable, default `x-goog-authenticated-user-email`) is the identity. Namespace membership comes from a `GroupSource` interface; the first implementation is a mapping in the deployment config (`groups:` email or group -> namespaces, plus an `admins:` list). A directory-backed source can replace it without touching callers. | The proxy in front of the platform already authenticates people. A directory sync is the heaviest part of the sibling and is not needed to serve six apps. |
| D16 | Machine callers | A separate lane: an OIDC ID token from a configured issuer, a dedicated audience, an allow-list of subjects, and an `acting_email` naming the human on whose behalf the call is made. The lane can only write inside namespaces the deployment config pins to that subject. | The assembly room writes secrets on a person's behalf. Machines must not use the human lane, and a machine must not be able to reach every namespace because it can reach one. |
| D17 | Scoping | One OpenBao namespace per owning group. One path prefix per app: `kv/apps/<app>/<purpose>`. One ESO identity per app: policy `eso-<app>-readonly` on that prefix plus token-self grants, JWT role `eso-<group>-<app>` with `bound_subject` pinned to a dedicated service account in the ESO namespace and `token_no_default_policy` on, a `ClusterSecretStore` `ladon-<group>-<app>` with `namespace: <group>` and a selector on the Kubernetes namespace's immutable `kubernetes.io/metadata.name` label, and `ExternalSecret`s whose `target.name` is the Secret name the consumer already uses. | Each line is a lesson the sibling paid for: the namespace field is what makes JWT login route to the team; the immutable label is what a rogue namespace cannot forge; the no-default flag is what stops a widened default policy from making every scoped identity decorative; the team-qualified identity name is what stops one group's ESO identity from authenticating as another's; keeping the Secret name is what keeps every consumer untouched. |
| D18 | Cluster access | The backend manages the cluster it runs on, through its own service account and RBAC, applying manifests with `kubectl apply --server-side --field-manager=ladon`. Other clusters are out of scope for this stage. | `kubectl` is already required for the install layer; a second client library for the same job is weight. One cluster is the brief. |
| D18a | ESO on a shared cluster | ESO is cluster-scoped and singular. If an operator is already installed, Ladon reuses it: it records the version, probes the CRD API version, and skips its own install. Ladon's stores are named `ladon-<group>-<app>` and never touch stores it did not create. The per-app service accounts live in Ladon's own namespace, not the operator's, and the JWT role's `bound_subject` names that namespace. | Two operators fight over one set of CRDs. A dedicated service-account namespace that only the platform operator can write to gives the same protection the sibling got from the operator's namespace, without writing into a namespace another team owns. |

Superseded: the "dry-run is a diff" promise in D3 above becomes true; the
`plan` command remains a pure render and `apply` without `--apply` connects and
diffs.

### Considered alternatives

- **GitOps apply model (ArgoCD or Flux).** Declarative and drift-resistant, and
  it matches parts of the sibling ecosystem. Rejected: Sero's agents deploy and
  maintain state directly, which is simpler to operate and aligns with the
  imperative nature of OpenBao day-2 tasks. We are not introducing a
  reconciler.
- **External storage backend (for example a database or object store).**
  Rejected: it adds an operational dependency for no benefit over Raft for the
  target deployments.
- **Extending OpenBao's built-in UI.** Rejected as the primary surface: it
  couples Sero to OpenBao's frontend and does not cover cross-cluster or
  cloud-sync views. The built-in UI remains available to operators alongside the
  wrapper.
- **Single-language (Go or TypeScript end to end).** Go fits the operator
  ecosystem but diverges from the Python siblings and is heavier for the UI and
  agent layers; TypeScript end to end is cohesive but a weaker fit for some
  Kubernetes and cloud tooling. Polyglot per layer was chosen for ecosystem fit.
- **React for the UI instead of Svelte.** React has a larger contributor pool,
  which matters for an open-source project, and the design tooling leans React.
  Rejected: the source UI is already a substantial, modern Svelte 5 application,
  so adopting Svelte reuses working code rather than rebuilding it, and Svelte
  fits an admin/status surface well. The contributor-pool tradeoff is accepted.
- **GitOps apply model.** See the apply model entry above; not introducing a
  reconciler.

## Architecture

Sero has a UI/agent layer, a backend-and-tooling layer, and the operated
software (OpenBao and ESO) beneath them. Each is independently deployable and
talks to the next through stable interfaces.

```
       operators (humans)            workloads (in cluster)        agents (MCP)
             |  OIDC                        |  JWT auth                  |  OAuth 2.1
             v                              v                            v
        +-------------------------------------------------------+   +-----------+
        |  Sero wrapper UI / admin surface (Svelte 5)          |   | MCP tools |  D12
        +-------------------------------------------------------+   +-----------+
                  |  HTTPS (OpenBao API + Sero backend API)              |
                  v                                                       v
        +-------------------------------------------------------+   +-----------+
        |  Sero backend and tooling (Python)                   |---| Postgres  |  D11
        |  1 Terraform: cloud substrate            D10          |   | audit,    |
        |  2 Helm: OpenBao install                 D3a          |   | tokens,   |
        |  3 SDK/kubectl: config + runtime + day-2 D3b          |   | grants    |
        |  dry-run + diff by default at each layer  D3          |   |           |
        +-------------------------------------------------------+   +-----------+
                  |                                  |
                  v                                  v
        +---------------------+           +-------------------------+
        |  OpenBao (Raft HA,  |  D1 D2    |  External Secrets Op.   |  D4
        |  KMS auto-unseal),  |  <------- |  ExternalSecret /       |
        |  KV, policies, auth |  reads    |  (Cluster)SecretStore   |
        +---------------------+           +-------------------------+
                                                     |
                          +--------------------------+--------------------------+
                          v                          v                          v
                   Kubernetes Secrets        GCP Secret Manager         AWS Secrets Manager   D5
```

- **Wrapper UI** is the secrets view and admin surface. It authenticates
  operators (OIDC) and never displays secret values; it shows key names, paths,
  namespaces, and sync status.
- **Backend and tooling** performs the privileged operations in three layers:
  Terraform for the cloud substrate (D10), a direct Helm install of OpenBao
  (D3a), and the OpenBao SDK plus `kubectl` for configuration, runtime secret
  operations, and ESO resource rendering (D3b). Every write defaults to dry-run
  with a diff and prints namespace, path, and key names (never values) before
  applying. It persists non-secret state (audit, tokens, grants) in PostgreSQL
  (D11) and exposes selected operations as MCP tools (D12).
- **OpenBao** is the source of truth for secrets, running HA on Raft with KMS
  auto-unseal.
- **ESO** reads from OpenBao and writes to the configured targets. It
  authenticates to OpenBao with a JWT auth role.

## Data and state model

- **Secret values** live only in OpenBao. They are never stored in this repo,
  never logged, and never rendered in the UI. ESO moves values from OpenBao to
  targets; Sero orchestrates and observes that movement without holding values.
- **Configuration** (which namespaces, policies, auth roles, and ESO resources a
  deployment has) is declarative input to Sero, expressed as parameterized
  config. It contains names, paths, and references, never secret values.
- **Operational state** under the agent-driven apply model (D3) lives across the
  three layers: Terraform state for the cloud substrate, the Helm release state
  for the OpenBao install, and live OpenBao state for configuration. Because
  there is no GitOps reconciler, the per-layer dry-run is the diff: a Terraform
  plan, a `helm diff`, and an SDK/`kubectl` diff, each comparing intended against
  live before any write. This is how drift stays visible without a reconciler.
- **Backend state** (audit log, MCP token families, step-up reveal grants,
  namespace-request workflow) lives in PostgreSQL (D11). It is operational
  metadata, never secret values.
- **Fleet and sync inventory** (which clusters are registered, their ESO install
  status, and which namespaces sync to which targets) is also operational
  metadata and lives in PostgreSQL (D11). The source scattered this across
  object-store blobs separate from its database, which made it a single point of
  failure with no audit trail; Sero keeps it in the one backend datastore
  alongside the rest of the operational state. It is derived state, rebuildable
  by re-querying the fleet and clusters, never the source of truth for secrets.
- **Rendered artifacts** (Terraform plans, Helm values, OpenBao policies, ESO
  `ExternalSecret` and `(Cluster)SecretStore` manifests) are produced from config
  and the opinionated defaults, and are covered by the contract tests in
  [testing.md](testing.md).

## Apply model in detail

The order in which the layers are brought up, with the prerequisites between
phases, is its own document: [deployment-order.md](deployment-order.md). This
section describes how a single apply works within that order.

Agent-driven apply (D3) works like this:

1. A change is expressed as config (parameters) plus the opinionated defaults.
2. Sero renders the concrete artifacts for the relevant layer: Terraform for the
   cloud substrate; pinned Helm chart and values for the OpenBao install; OpenBao
   policies, namespaces, auth, and ESO resources for the config layer.
3. A dry-run shows what would change, per layer: a Terraform plan for the
   substrate, a `helm diff` for the install, and an SDK/`kubectl` diff for config
   and runtime, showing namespace, path, key names, and sync target. No secret
   values appear. The config layer reads each live object to produce that diff,
   so every operation reports create, update with the fields that differ, or
   already current, rather than what it intends to send.
4. After explicit confirmation, Sero applies the relevant layer: `terraform
   apply` for the substrate, `helm upgrade --install` for OpenBao, and the
   OpenBao SDK / `kubectl` for configuration, secret, and ESO operations. The
   config layer writes only what differs, so a repeated apply converges instead
   of failing, and reads each object back afterwards: a write that does not land
   fails loudly rather than reporting success.
5. Day-2 operations (init, unseal, snapshot, migration) are explicit, audited
   operations in the tooling, not background reconciliation.

This keeps the model simple and the blast radius of any write visible before it
happens, consistent with the secrets-hygiene rules in
[../CONTRIBUTING.md](../CONTRIBUTING.md).

## Security and secrets hygiene

These are first-class and pervade the design:

- Secret values never enter the repo, logs, or UI. Where a key must be shown to
  exist, it is masked and treated as internal.
- Writes default to dry-run and show names only, then require confirmation.
- Placeholder values are refused.
- Least privilege: the baseline policies grant the narrowest access that works,
  and per-deployment policies extend from there explicitly.
- Auth is OIDC for humans and JWT for workloads (D6); no shared static
  tokens for routine use.

The generic baseline of namespaces, policies, and auth methods, and the
customization model around them, are designed in Phase 1 after the source repos
are read. The principle is a small handful of generic, well-named defaults with
everything deployment-specific driven by configuration.

## Roadmap

The phased plan is summarized in [../ROADMAP.md](../ROADMAP.md). In short: Phase
0 foundations (this repo's bootstrap), then read and document the source repos
and design the generic baseline, then the OpenBao deploy baseline, then ESO and
sync, then the wrapper UI, then the agent and multi-environment hardening.
