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

Creating the team namespaces is part of the root config step: for every team in
the config, Ladon ensures `sys/namespaces/<namespace>` exists and then, inside
it, enables the KV v2 mount and writes the policies the team's configured
access level grants (`team-readwrite`, or `team-readonly` for a team configured
read-only) plus `external-secrets-read`, which every team needs to sync secrets
and which grants no write. A namespace is only ever created, never deleted or
renamed, because everything the team holds lives inside it. Each operation
carries the namespace it runs in, so one connection configures the root and
every team, and the same mount name in two namespaces stays two separate
objects.

Because root config now creates namespaces and writes inside them, the token it
runs with needs more than root-level configuration rights: it must be able to
create namespaces (`sys/namespaces/*`) and to mount engines and write policies
within each one. On a fresh install that is the initial root token; afterwards
it is whatever policy an operator is given for this step.

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
| `external-secrets-read` | one team namespace | read/list on the namespace's KV data and metadata, nothing else: the per-team ESO read role |
| `external-secrets-root` | root, cross-namespace | read/list on the KV data and metadata of every team namespace, for a centralized ESO that pushes to cloud secret managers |
| `eso-<app>-readonly` | one application's prefix in one team namespace | read on that application's KV data and metadata, plus the four token self paths, and nothing else (see [Per-application identities](#per-application-identities)) |

The first two are the per-team baseline. `external-secrets-read` is the per-team
ESO read policy; `external-secrets-root` is the platform-level cross-namespace
read used only when cloud secret-manager sync is turned on. Both are read-only.

Policies grant the narrowest access that works (least privilege, per
[../CONTRIBUTING.md](../CONTRIBUTING.md)). Per-deployment policies extend from
these templates explicitly; the templates themselves are not edited per team.

## Auth methods

Baseline:

- **JWT** for in-cluster workloads. This is the primary workload auth: the
  External Secrets Operator authenticates with a projected service account token,
  and OpenBao verifies it against the cluster's published keys (a discovery URL),
  so no secret is configured. Each cluster gets its own JWT mount bound to its
  issuer, which covers the cross-cluster case in the baseline rather than as an
  add-on.
- **OIDC** for human operators, against a generic, parameterized OIDC provider
  (discovery URL, client, group claim, and the group-to-namespace mapping all
  come from config). The source's specific identity provider becomes one
  possible value, not a hardcoded assumption.

Optional, documented but not enabled by default:

- **AppRole** for service-to-service automation that has no workload identity.

A deployment that only needs operator and workload access runs with the two
baseline methods; AppRole is turned on through configuration when needed.
Kubernetes auth is intentionally not in the baseline: the source treats it as
legacy and is moving workloads to JWT, so Ladon starts where that migration
lands.

## Secret engine

KV version 2, one mount per namespace, mounted at a conventional path. This is
the only engine in the baseline. Other engines (PKI, database, transit) are out
of scope for the baseline and can be added per deployment later.

## Per-application identities

A team namespace is not a small enough boundary on its own. It holds every
application the team runs, so one identity for the whole team can read all of
them, and a single compromised workload reads its neighbours' secrets. The
baseline therefore puts the boundary at the application.

Each application gets one path prefix inside its team's KV mount,
`kv/apps/<app>/<purpose>`, and four objects that all name each other:

- a policy `eso-<app>-readonly` in the team's namespace, granting read on
  everything under the application's data prefix (`kv/data/apps/<app>/*`), read
  on the bare data path itself (`kv/data/apps/<app>`, which the glob does not
  match), read and list on its metadata prefix (`kv/metadata/apps/<app>/*`), and
  the four self paths a token needs to look itself up, renew itself, revoke
  itself, and answer a capability check. Nothing else;
- a JWT role `eso-<team>-<app>-<digest>` on the cluster's `jwt-<cluster>` mount,
  carrying only that policy, refusing the namespace's `default` policy, bound to
  the audience the store asks for, and bound by subject to one service account;
- a `ServiceAccount eso-<team>-<app>-<digest>` in Ladon's own Kubernetes
  namespace (not the secrets operator's, which is shared), with token
  automounting off;
- a `ClusterSecretStore ladon-<team>-<app>-<digest>` naming the team's OpenBao
  namespace, the JWT mount, the role, and that service account, and admitted
  into exactly one Kubernetes namespace by the API-server-set
  `kubernetes.io/metadata.name` label, which a namespace owner cannot forge.

Every name that is visible outside the team's namespace carries the team, so
one team's application cannot claim the identity of another team's application
of the same name. It also carries `<digest>`, the first six hex characters of a
SHA-256 of the team and application names: both parts may contain hyphens, so
joining them with one is ambiguous on its own (team `a-b` with application `c`
and team `a` with application `b-c` both read as `a-b-c`), and those two live in
different OpenBao namespaces but share one cluster. The digest disambiguates the
pair; it is not a security boundary. It is derived from the two names and
nothing else, so onboarding the same application again never renames it.

Applications are onboarded by command, one at a time; they are not listed in the
deployment config, because an application is a thing a team adds, not a fact
about the platform. The command does need the deployment config, because the
team's OpenBao namespace and the KV mount are facts about the platform and
guessing either renders an identity that grants access to a path nothing uses.

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
