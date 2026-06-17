# Generic baseline and customization model

This document defines the small, generic baseline Ladon ships and the
configuration model that customizes it per deployment. It is the design gate the
project set before any feature or infrastructure code lands: build this, review
it, then implement against it.

It follows from the architecture in [design.md](design.md) and the source review
in [source-review.md](source-review.md). The guiding principle: sensible generic
defaults, with everything deployment-specific driven by clear configuration, and
nothing company-specific hardcoded.

## Why a baseline at all

The source deployment carries roughly three dozen team namespaces, five auth
methods, and seven policy types, all wired to one organization's structure. That
is the opposite of what Ladon wants. Ladon ships the smallest set that a clean
deployment needs and grows from configuration, so a new adopter can stand it up
with almost nothing and add their own teams without editing Ladon itself.

## Namespaces

OpenBao's open-source build supports namespaces (added in the 2.3 line; Ladon
pins a later release in [versions.md](versions.md)). A namespace has its own auth
methods, secret engines, policies, and tokens, and tokens do not cross
namespaces. This is the isolation primitive Ladon uses, the same model the source
relies on, and it is available without an enterprise license.

The baseline namespaces are deliberately minimal:

- **root** - the platform space. Holds platform-wide configuration: the audit
  devices, the operator OIDC method, and the cross-namespace read role that the
  External Secrets Operator uses. Operators administer Ladon here.
- **team namespaces** - created entirely from configuration. The baseline ships
  with zero hardcoded teams. A deployment lists its teams in config (see
  [Customization model](#customization-model)) and Ladon creates a namespace per
  team, each with the standard policy set, auth roles, and a KV mount.

That is the whole baseline: a platform root plus config-driven team namespaces.
No operations/backend/frontend grouping, no team-specific names in the repo.

## Policies

Ladon ships a few generic policy templates, each rendered per namespace from
parameters (the namespace path and the KV mount). The shapes are distilled from
the source; the bindings come from config.

| Template | Scope | Grants |
| --- | --- | --- |
| `team-readwrite` | one team namespace | create/read/update/delete/list on the namespace's KV mount, plus manage the namespace's own policies and workload auth roles |
| `team-readonly` | one team namespace | read/list on the namespace's KV mount |
| `external-secrets-read` | root, cross-namespace | read on the KV data paths the sync layer needs, nothing else |
| `audit-reader` | root | read access to audit information for operational tooling |
| `secrets-syncer` | root | read on the KV paths a sync workflow consumes |

The first two are the per-team baseline. The last three are platform-level and
optional, enabled when the matching capability (ESO sync, audit tooling) is
turned on.

Policies grant the narrowest access that works (least privilege, per
[../CONTRIBUTING.md](../CONTRIBUTING.md)). Per-deployment policies extend from
these templates explicitly; the templates themselves are not edited per team.

## Auth methods

Baseline:

- **OIDC** for human operators, against a generic, parameterized OIDC provider
  (discovery URL, client, group claim, and the group-to-namespace mapping all
  come from config). The source's specific identity provider becomes one
  possible value, not a hardcoded assumption.
- **Kubernetes** for in-cluster workloads.

Optional, documented but not enabled by default:

- **JWT** for the External Secrets Operator, including the cross-cluster case.
- **AppRole** for service-to-service automation.

A deployment that only needs operator and workload access runs with the two
baseline methods; the others are turned on through configuration when needed.

## Secret engine

KV version 2, one mount per namespace, mounted at a conventional path. This is
the only engine in the baseline. Other engines (PKI, database, transit) are out
of scope for the baseline and can be added per deployment later.

## Customization model

A deployment is described by configuration, not by edits to Ladon. The
configuration has three parts:

1. **Deployment settings** - the per-environment platform facts: cloud provider
   and project or account, region, DNS domain, the KMS key used for auto-unseal,
   the OIDC provider settings, the identity-group naming convention, and which
   sync targets are enabled. All parameterized, no defaults that name a company.

2. **Teams** - a list, each entry naming a team, its namespace, the environments
   it exists in, its identity-provider groups, and which policy templates apply.
   This is the seam the source already moved toward (a declarative team list
   feeding a reusable module); Ladon generalizes it. An empty list is valid: a
   deployment with no teams still has a working platform root.

3. **Sync targets** - which of the supported targets (Kubernetes, and the cloud
   secret managers) are enabled, and their per-target parameters. Disabled by
   default; each is opt-in.

This configuration drives the three apply layers from the apply model in
[design.md](design.md): Terraform for the cloud substrate (cluster, KMS,
networking, snapshot buckets, monitoring), a direct Helm install of OpenBao, and
the OpenBao SDK for the per-team namespaces, policies, auth, and KV mounts. The
agent applies each layer with dry-run and a diff first. Note that the OpenBao
install is a Helm step, not a Terraform module, and the per-team configuration is
applied through the SDK, not the Terraform Vault provider, so each layer keeps an
independent lifecycle.

### A minimal deployment

The smallest useful Ladon deployment:

- One environment.
- OpenBao installed on Raft with KMS auto-unseal.
- The platform root with audit and the operator OIDC method.
- Zero teams (added later from config).
- No sync targets enabled.

From there, an adopter adds teams and turns on sync targets entirely through
configuration. Nothing in this path names a company, a domain, or a cloud
account; those are all values the adopter supplies.

## What this baseline deliberately leaves out

- The three dozen source team namespaces and their grouping.
- Provider-specific assumptions baked into defaults (identity provider, cloud
  project naming, cluster names, domains).
- The ticketing and pull-request integrations from the source onboarding flow;
  those become optional, pluggable hooks rather than baseline behavior.
- Engines beyond KV v2.

## Next

With this baseline reviewed and merged, implementation can begin against it,
following the phase order in [../ROADMAP.md](../ROADMAP.md): the OpenBao deploy
baseline first, then sync, then the UI and agent surface. Each piece is pulled
from the sources generalized and branding-scrubbed, one reviewed pull request at
a time.
