# The platform vault: Ladon running for real -- design

Status: approved 2026-09-22 (operator delegation: build to a working state). Records
decisions D13 to D18, which extend [../design.md](../design.md). The plan is
[../plans/2026-09-22-platform-vault.md](../plans/2026-09-22-platform-vault.md).

## Why

Ladon's code is complete in its pure layers and has never run for anyone. The first
live run (2026-09-22, OpenBao 2.5.5 from this repo's pinned chart, `apply --phase
root-config`) applied cleanly once and failed on the second run at the first
operation: the apply layer never reads live state, so it can neither converge nor
verify. The sibling platform this repo generalises spent most of its fix history on
exactly the things Ladon does not do yet: read back what you wrote, scope a store to
one namespace, drop the default policy from every role, never let a write path
exist that a guard does not cover.

The goal is narrow: one working OpenBao, operated by Ladon, serving the apps of one
agent platform (an assembly room, a tool gateway, a switchboard, an authorization
service, an observability platform, and the agents themselves), on the cluster they
already run on. Lighter than the sibling. Expandable later.

## Decisions

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

Superseded: the "dry-run is a diff" promise in design.md D3 becomes true; the
`plan` command remains a pure render and `apply` without `--apply` connects and
diffs.

## What "working" means

1. `ladon apply` converges: a second run reports every operation already current and
   writes nothing; a hand-edited policy shows as a diff and is repaired; every write
   is read back and a mismatch is an error naming the field.
2. A team's namespace, KV mount and policies exist because the deployment config
   says so, created by Ladon.
3. ESO on the platform cluster reads from the vault through a store Ladon rendered
   and applied, and only the intended Kubernetes namespace can use that store. The
   negative test (a store reference from another namespace) fails.
4. The backend runs, behind the proxy, serving the UI's thirteen routes over the
   datastore and the vault, with migrations applied at boot.
5. The backend is deployed on the platform cluster from an image this repo builds,
   with a deploy contract the deploy platform accepts.
6. One app is onboarded through the endpoint and passes the acceptance matrix:
   cross-prefix read is refused, a store reference from another namespace is
   rejected, a login from the wrong service account fails, a re-run is a no-op.
7. The role audit exists and reports clean.
8. The assembly room can write one app's secret through the machine lane and the
   app's pod reads it as the same Kubernetes Secret it always did. The switch that
   makes the room use this path is a configuration flag, off by default.

## Out of scope for this stage

The OAuth 2.1 server and the MCP mount (the machine lane above is REST; the MCP
tools keep working in-process for the CLI); cloud secret-manager sync targets; the
reveal step-up (the UI's reveal stays behind the pluggable gate, default deny in a
deployed backend); fleet inventory and a second cluster; KMS auto-unseal; a
directory-backed group source.
