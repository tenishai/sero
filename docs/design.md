# Ladon design

This document describes Ladon's architecture and the decisions behind it. It is
the reference for why the system is shaped the way it is. The phased plan that
follows from it lives in [../ROADMAP.md](../ROADMAP.md).

Ladon is an open-source secrets and vault management platform built on OpenBao.
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

- Forking OpenBao or the External Secrets Operator. Ladon integrates and
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
| D2 | Storage backend | Integrated Storage (Raft) | The recommended production backend. No external dependency, built-in HA, and snapshot backup/restore. Keeps Ladon easy to deploy: nothing to provision but OpenBao itself. |
| D2a | Seal / unseal | Auto-unseal via a cloud KMS (pluggable) | Auto-unseal removes manual key-share handling and is the practical default for HA. The KMS provider (GCP Cloud KMS, AWS KMS, others) is parameterized per deployment. Manual/Shamir unseal remains supported for deployments without a cloud KMS. |
| D3 | Apply model | Agent-driven, declarative-then-direct (Terraform + Helm + SDK) | Ladon's agents deploy and maintain state themselves. Terraform is the primary infrastructure-as-code layer (cluster, OpenBao install via the Helm provider, and OpenBao configuration: namespaces, policies, auth methods, KV mounts via the Vault/OpenBao provider). The SDK and `kubectl` handle runtime operations (secret read/write, ESO resource rendering and apply) and day-2 tasks (init, unseal, snapshot, migration). Every write defaults to dry-run with a diff against live state. There is no GitOps reconciler; the agent owns the plan, the diff, and the apply. GitOps is recorded as a considered, rejected alternative below. |
| D4 | Secret sync | External Secrets Operator | The de facto standard for syncing secrets into Kubernetes and between secret stores. Broad provider support including GCP Secret Manager and AWS Secrets Manager. |
| D5 | Sync targets | Kubernetes, GCP Secret Manager, AWS Secrets Manager | The three targets the platform must serve. Each is an ESO `SecretStore` / `ClusterSecretStore` provider, parameterized per deployment. |
| D6 | Auth methods | Baseline Kubernetes and OIDC; JWT and AppRole available | Kubernetes auth for in-cluster workloads and OIDC for human operators are the generic, provider-agnostic baseline. ESO authenticates with a JWT or Kubernetes auth role. AppRole is available for service-to-service automation. The source deployment uses five methods; Ladon's baseline is the two, with the others as documented, optional additions. |
| D7 | UI | Custom wrapper application, Svelte 5 | A separate UI that spans multiple clusters, ESO sync status, and the cloud secret stores. OpenBao's built-in UI is single-cluster and CRUD-focused and cannot show the cross-cluster and cloud-sync views that are Ladon's reason to exist. Svelte 5 is chosen to reuse and generalize the existing source UI rather than rebuild it; it is a strong fit for an admin/status surface. |
| D8 | Languages | Polyglot: Python and TypeScript/Svelte | Python for the backend, operational tooling, and the agent/MCP surface (consistent with the sibling repos and a strong fit for Kubernetes and cloud SDKs). TypeScript/Svelte for the wrapper UI (D7). |
| D9 | TLS / certificates | cert-manager (candidate) | For issuing and rotating OpenBao and ingress certificates. Confirmed and pinned when the deploy baseline lands. |
| D10 | Infrastructure as code | Terraform with pinned providers | The primary apply mechanism (D3): the GKE/cluster resources, the OpenBao Helm install, OpenBao configuration (namespaces, policies, auth, KV mounts), and the cloud-side resources (IAM, secret stores, networking). Providers pinned in versions.md. |
| D11 | Backend datastore | PostgreSQL | The backend needs durable state that is not secret material: audit log, MCP token families, step-up reveal grants, and namespace-request workflow. None of it is secret values. PostgreSQL is the conventional, portable choice. |
| D12 | Agent / MCP surface | MCP server exposing selected operations | Selected OpenBao operations are exposed as MCP tools (OAuth 2.1), with secrets-hygiene behavior baked in (dry-run, names only, refuse placeholders). This is how Ladon fits the agent ecosystem; built with the mcp-builder skill. |

### Considered alternatives

- **GitOps apply model (ArgoCD or Flux).** Declarative and drift-resistant, and
  it matches parts of the sibling ecosystem. Rejected: Ladon's agents deploy and
  maintain state directly, which is simpler to operate and aligns with the
  imperative nature of OpenBao day-2 tasks. We are not introducing a
  reconciler.
- **External storage backend (for example a database or object store).**
  Rejected: it adds an operational dependency for no benefit over Raft for the
  target deployments.
- **Extending OpenBao's built-in UI.** Rejected as the primary surface: it
  couples Ladon to OpenBao's frontend and does not cover cross-cluster or
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

Ladon has a UI/agent layer, a backend-and-tooling layer, and the operated
software (OpenBao and ESO) beneath them. Each is independently deployable and
talks to the next through stable interfaces.

```
       operators (humans)            workloads (in cluster)        agents (MCP)
             |  OIDC                        |  Kubernetes auth           |  OAuth 2.1
             v                              v                            v
        +-------------------------------------------------------+   +-----------+
        |  Ladon wrapper UI / admin surface (Svelte 5)          |   | MCP tools |  D12
        +-------------------------------------------------------+   +-----------+
                  |  HTTPS (OpenBao API + Ladon backend API)              |
                  v                                                       v
        +-------------------------------------------------------+   +-----------+
        |  Ladon backend and tooling (Python)                   |---| Postgres  |  D11
        |  - Terraform (IaC: cluster, OpenBao install + config) |   | audit,    |
        |  - Helm / SDK / kubectl (runtime + day-2 ops)         |   | tokens,   |
        |  - dry-run + diff by default on writes        D3 D10  |   | grants    |
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
- **Backend and tooling** performs the privileged operations: Terraform for the
  cluster, the OpenBao Helm install, and OpenBao configuration (namespaces,
  policies, auth, KV mounts), plus the SDK and `kubectl` for runtime secret
  operations and ESO resource rendering. Every write defaults to dry-run with a
  diff and prints namespace, path, and key names (never values) before applying.
  It persists non-secret state (audit, tokens, grants) in PostgreSQL (D11) and
  exposes selected operations as MCP tools (D12).
- **OpenBao** is the source of truth for secrets, running HA on Raft with KMS
  auto-unseal.
- **ESO** reads from OpenBao and writes to the configured targets. It
  authenticates to OpenBao with a JWT or Kubernetes auth role.

## Data and state model

- **Secret values** live only in OpenBao. They are never stored in this repo,
  never logged, and never rendered in the UI. ESO moves values from OpenBao to
  targets; Ladon orchestrates and observes that movement without holding values.
- **Configuration** (which namespaces, policies, auth roles, and ESO resources a
  deployment has) is declarative input to Ladon, expressed as parameterized
  config. It contains names, paths, and references, never secret values.
- **Operational state** under the agent-driven apply model (D3) is the live
  state of OpenBao and the clusters, plus Terraform state for the
  infrastructure-as-code layer. Because there is no GitOps reconciler, the
  Terraform plan and the SDK/`kubectl` dry-run are the diff: they compare the
  intended config against live before any write. This is how drift stays visible
  without a reconciler.
- **Backend state** (audit log, MCP token families, step-up reveal grants,
  namespace-request workflow) lives in PostgreSQL (D11). It is operational
  metadata, never secret values.
- **Rendered artifacts** (Terraform plans, Helm values, OpenBao policies, ESO
  `ExternalSecret` and `(Cluster)SecretStore` manifests) are produced from config
  and the opinionated defaults, and are covered by the contract tests in
  [testing.md](testing.md).

## Apply model in detail

Agent-driven apply (D3) works like this:

1. A change is expressed as config (parameters) plus the opinionated defaults.
2. Ladon renders the concrete artifacts: Terraform for the cluster, the OpenBao
   install, and OpenBao configuration (namespaces, policies, auth, KV mounts);
   Helm values; and ESO resources.
3. A dry-run shows what would change: a Terraform plan for infrastructure and
   config, and an SDK/`kubectl` diff for runtime resources, showing namespace,
   path, key names, and sync target. No secret values appear.
4. After explicit confirmation, Ladon applies: `terraform apply` for the IaC
   layer, and the OpenBao SDK / `kubectl` for runtime secret and ESO operations.
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
- Auth is OIDC for humans and Kubernetes for workloads (D6); no shared static
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
