# Source review

Sero is rebuilt from two existing source repositories. They are read-only
sources, not dependencies: Sero owns a new, clean, generic implementation and
pulls the useful parts across, file by file, scrubbed of all company branding
and slimmed to a generic baseline.

This document records what those sources contain, the seams Sero builds on, and
the scrub-and-generalize plan. It is the reference for the Phase 1 work in
[../ROADMAP.md](../ROADMAP.md) and complements the architecture in
[design.md](design.md).

Two mandates apply to everything brought over:

1. **Scrub all company branding.** No company or product names, domains, cloud
   project or account IDs, region or cluster names, identity-provider org names,
   or internal identifiers survive in Sero. This document follows the same
   rule: the scrub surface below is described as categories and placeholder
   patterns, never real values.
2. **Generalize and slim down.** The sources are client-specific and sprawling.
   Sero keeps a small handful of generic namespaces, policies, and auth
   methods, with everything deployment-specific driven by configuration.

## Source A: OpenBao infrastructure (Terraform on Kubernetes)

### Purpose and shape

Terraform end to end (the large majority of the repo is `.tf`), with remote
state in cloud object storage. It provisions and operates OpenBao clusters
across four environments (dev, qa, prod primary, and a prod disaster-recovery
standby) on managed Kubernetes.

- OpenBao is installed via the Terraform Helm provider (the upstream OpenBao
  Helm chart) and configured via the Terraform Vault/OpenBao provider
  (namespaces, policies, auth methods, KV mounts).
- This confirms design decision D3/D10: the apply model is agent-driven
  Terraform, not GitOps.

### Storage, seal, and disaster recovery

- **Storage:** Integrated Storage (Raft), three-node HA with autopilot
  (confirms D2).
- **Seal:** auto-unseal via a cloud KMS (confirms D2a). Key material is a
  parameterized keyring/key, never in the repo.
- **DR:** the standby environment is kept current by scheduled Raft snapshots to
  object storage and a restore job, reached over a cross-cluster gateway.
  Failover is a documented, DNS-level cutover.

### Configuration inventory (to be slimmed)

- **Namespaces:** roughly three dozen team namespaces in the source, grouped
  into broad categories (operations, backend, frontend). This is client-specific
  sprawl. Sero's baseline keeps only a small handful of generic namespaces.
- **Policies:** about seven distinct policy types (a full-access team policy, a
  read-only variant, an external-secrets read policy, and a few operational
  roles for audit-log reading and sync). The shapes generalize well; the count
  and the team-specific bindings do not.
- **Auth methods:** five in the source (OIDC via an identity provider,
  Kubernetes, a JWT method for ESO, AppRole, and a root JWT method). Sero's
  baseline is JWT for workloads plus OIDC for operators, with AppRole as a
  documented optional addition and Kubernetes auth deliberately left out, since
  the source treats it as legacy and is migrating away from it (D6).
- **Secret engines:** KV v2 only, one mount per namespace.

### The generic seam to build on

The source has a newer canonical pattern that is the right foundation for
Sero's customization model:

- A single declarative input file lists teams, and a reusable module creates,
  per team, the namespace plus its policy, OIDC role, identity-group bindings,
  and KV mount in one place.

Sero generalizes this: a parameterized config file (teams, environments,
identity-provider groups) drives a small set of reusable modules, with no
hardcoded org structure.

### Reusable modules worth generalizing

The OpenBao Helm install module, the per-team module described above, the KMS
module, the snapshot and restore modules, the monitoring module, and the
private-connectivity module. Each needs its company-specific defaults replaced
with variables.

### Scrub surface (categories only)

Company-specific values cluster in the environment root configs, the ESO
provider locals, and the team module. The categories to parameterize:

- Cloud project and account identifiers, regions, and networks.
- DNS domains and hostnames.
- Identity-provider org URL and group-name conventions.
- Cluster, node-pool, service-account, KMS-key, and object-storage-bucket names.
- Monitoring webhook and channel identifiers (make optional).

No secret values are hardcoded in the source: sensitive data is read from a
cloud secret store at apply time. Sero preserves that property.

### External Secrets Operator note

ESO is deployed from a different repository (via a GitOps controller there) and
is mid-migration between major versions. In the source, the OpenBao clusters act
as push hubs: ESO reads from OpenBao KV and pushes to cloud secret stores. Sero
pins the ESO version itself when the sync layer lands (see
[versions.md](versions.md)); the exact pin stays `tbd` until then.

## Source B: secrets UI and backend wrapper

### Purpose and shape

A wrapper that sits on top of the OpenBao deployments: a Python backend, a
Svelte UI, a relational datastore, and an agent (MCP) surface.

- **Backend:** FastAPI, talking to OpenBao through the SDK, rendering ESO
  resources and applying them with `kubectl`, and syncing to Kubernetes and the
  cloud secret stores. Confirms the Python choice (D8) and the SDK/`kubectl`
  runtime path (D3).
- **UI:** Svelte 5 (Vite, Tailwind), a single-page admin and secrets-view
  surface. This is the basis for adopting Svelte 5 (D7) rather than rebuilding
  in React.
- **Datastore:** PostgreSQL, holding non-secret operational state (audit log,
  agent token families, step-up reveal grants, and a namespace-request
  workflow). Confirms D11.
- **Agent surface:** an MCP server (OAuth 2.1) exposing selected operations.
  Confirms D12.

### Secrets hygiene already present (preserve)

The source already does several things Sero mandates, and these are worth
carrying over rather than reinventing:

- Listing reads metadata only; values are not returned by default.
- Plaintext reveal is gated behind a step-up re-authentication and a short-lived
  grant.
- A dry-run preview renders resources before any apply.
- Audit logging redacts values with a key-name allowlist plus an entropy
  heuristic, so secret material never reaches the log.

### Sync model

Path conventions encode the sync target (a Kubernetes path shape, a cloud
secret-manager path shape per provider, and a bare path for OpenBao-only). The
backend renders the matching ESO resources per target. Sero keeps the idea of
target-encoding path conventions, generalized and configurable rather than
hardcoded.

### Scrub surface and seams (categories only)

Company-specific configuration concentrates in two files: a backend settings
module and a small UI constants module (host names and default project lists).
These are the primary extraction seams: move the values out to configuration and
replace UI branding strings. Other company-specific couplings to generalize or
make optional:

- Identity-provider group to namespace mapping (generalize to a configurable
  claim or group lookup).
- A ticketing integration and a pull-request-opening integration used by the
  namespace-request workflow (make these pluggable hooks, not hardcoded).
- Cluster and project naming assumptions in the deployer (replace with discovery
  plus optional overrides).
- Product-name and brand strings in the UI (move to configuration).

## The generic baseline Sero will define

Before building, Phase 1 produces a baseline design. The direction, from this
review:

- **Namespaces:** a small handful of generic namespaces, not three dozen team
  namespaces. Per-deployment teams come from config.
- **Policies:** the generalized shapes only (a team read/write policy, a
  read-only policy, an external-secrets read policy, and the operational roles),
  with bindings driven by config.
- **Auth methods:** JWT for workloads and OIDC for operators as the baseline,
  with AppRole documented as optional and Kubernetes auth excluded.
- **Customization model:** a parameterized config (teams, environments,
  identity-provider settings, sync targets) drives reusable Terraform modules
  and the backend, with sensible generic defaults and nothing company-specific
  hardcoded.

## How the rebuild proceeds

Per the workflow in [../CONTRIBUTING.md](../CONTRIBUTING.md): one small,
single-purpose, reviewed pull request at a time. Each piece is pulled over
generalized and branding-scrubbed, never copied verbatim. Infrastructure modules
and the backend extraction seams come first; the UI and agent surface follow the
phase order in [../ROADMAP.md](../ROADMAP.md). No feature code lands until the
generic baseline design is reviewed.
