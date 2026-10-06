# Status, in detail

What is merged on main, with the PR for each piece, and what the live proof on a shared
development cluster covered. Moved here from the README verbatim on 2026-10-05; the README
keeps the summary.

All merged on main:

- Apply converges (create / update with a shown diff / already current), reads
  back every write, blocks instead of destroying; `plan` is offline, `apply`
  without `--apply` connects and diffs. (PR #59)
- Teams in config become OpenBao namespaces with the KV mount and team
  policies inside; team onboarding runs inside the namespace; per-application
  identities (`app-onboarding`): a read-only policy on the app's prefix, a JWT
  role pinned to a dedicated service account with no default policy, a
  ClusterSecretStore scoped to the OpenBao namespace and to the app's
  Kubernetes namespace by its immutable name label. (PR #61)
- Cluster documents applied with server-side apply under the `ladon` field
  manager, read back and verified with strict comparison under
  `spec.conditions` and the auth block, with an already-current signal; an
  operator already on the cluster is detected and reused; `install-eso`;
  `--phase sync` for one ExternalSecret. (PRs #60, #63)
- The HTTP backend: proxy-header or signed-assertion identity, config group
  source, the UI's routes, one environment per deployment, audit rows in
  their own session including denials and 401s, reveal behind a
  deny-by-default gate, migrations at boot, SQLite or PostgreSQL. (PR #62)
- Image (pinned by digest, helm/kubectl by checksum, non-root, read-only
  rootfs), deploy contract for the deploy platform (class gateway),
  standalone manifests, three NetworkPolicies including the link-local DNS
  rule, a CI image smoke. (PRs #64, #67)
- The role audit (`scripts/ladon audit`). (PR #65)
- The machine lane: `POST /api/service/secrets` and `/api/service/sync` per
  configured OIDC caller, pinned subject and namespaces, attribution via
  `acting_email`. (PR #66)
- The machine lane reads and deletes: `GET /api/service/secrets` for the key
  names, the version and when a path last changed; `DELETE` on both routes,
  removing a path with all of its versions and the ExternalSecret in front of
  it, which takes the Kubernetes Secret it owns with it. Dry run by default,
  idempotent, and guarded three ways: a cluster object another field manager
  owns is `409`, one belonging to another application or namespace is `403`,
  and the answer says whether a workload's Secret goes too. The lane can now
  delete; the room's destroy step is the remaining half.
- Apply phases as admin jobs with a restart sweep; namespace-request approval
  applies the team; MCP denial audit; the SQLite primary-key migration. (PR
  #68)

Proven live on a shared development cluster: a single-node vault installed
from the pinned chart; the full configuration rebuilt from config on a fresh
vault, with a second run writing nothing and a repointed store updated in
place; a hand-edited policy diffed and repaired; an existing External Secrets
Operator detected and reused; the round-trip vault -> store -> ExternalSecret
-> Secret, and the same ExternalSecret from another namespace refused; the
acceptance matrix with real projected tokens (own prefix readable, another
app's prefix and system paths and writes refused, a different service
account refused with invalid subject, re-onboarding a no-op); the audit
clean, then a widened default policy caught as HIGH, then clean after
repair; the backend deployed from the image in the cluster, migrating at
boot and serving the UI's routes through a port-forward; app onboarding run
as a job through the admin endpoint. The platform vault runs on cloud-KMS
auto-unseal, migrated in place from Shamir on the running vault and proven by
a pod delete it came back from unsealed on its own. The backend now sits
behind an identity-aware proxy on that cluster: a cloud external HTTPS load
balancer terminating a managed certificate at a reserved address, with the
proxy authenticating every caller and the backend verifying the signed
assertion rather than trusting a header.

Not yet: no OAuth 2.1 server / MCP mount over HTTP; no cloud secret-manager
sync; reveal step-up not built (gate denies by default); no fleet / second cluster; one process, one replica (the job runner and
the allow list are per process); no image build in CI, images are built by
hand from a clean export of a commit and tagged with its short sha.

See [ROADMAP.md](ROADMAP.md) for the phased plan.
