# Platform Vault Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ladon operates one dedicated OpenBao for an agent platform, end to end: convergent apply, namespaces from config, per-app scoped sync, a running backend serving the UI, deployed on the platform cluster, one app onboarded and proven, one consumer writing through the machine lane.

**Architecture:** `docs/specs/2026-09-22-platform-vault-design.md` (D13 to D18). The apply layer gains read-diff-write-verify; the config layer gains namespaces and per-app identities; a FastAPI backend serves the UI's existing routes over the datastore and the vault and runs apply operations as background jobs; a Dockerfile and a deploy contract make it deployable; the assembly room gets a client behind a flag.

**Tech Stack:** Python 3.14, hvac (pinned), FastAPI + uvicorn (pinned, new), SQLAlchemy + Alembic (present), `kubectl`/`helm` subprocesses, Svelte 5 UI (present), pytest, ruff.

## Global Constraints

- CONTRIBUTING.md rules are absolute: never a secret value anywhere (code, tests, fixtures, commit messages, PR text, logs); brand-neutral (no company names, domains, project ids, cluster names, regions that identify a deployment; `example` values only). `scripts/lint-conventions.sh` must pass.
- Pinned dependencies only; each new dependency is named in `docs/versions.md` with its pin and reason in the same commit.
- Every write to OpenBao or the cluster is read back and verified; a mismatch is an error naming the operation and the field. Every apply operation is idempotent: a second run reports `already current` and writes nothing.
- No token, URL with credentials, or secret material in any log line or error message. Errors name namespace/mount/path/field, never values.
- `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, `scripts/lint-conventions.sh`, `scripts/validate-helm.sh` all pass before every commit. Never pipe a test or lint command into `tail`/`head`: redirect to a file, `echo "exit=$?"`, then read the file.
- Tests are offline: OpenBao and kubectl are faked at the client boundary. Live proofs are run by the operator (this session) against the scratch install and recorded in the CHANGELOG entry for the task.
- Commits are small and conventional (`feat(apply): ...`), end with a blank line then `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; `git add <named files>` only; one PR per task, squash-merged after review.
- Comments state constraints and reasons in the surrounding style; never narrate the change.

---

### Task 1: Convergent apply, pinned roles, scoped stores

Already dispatched. Produces: read-diff-write-verify for every op; `bound_issuer`; `token_no_default_policy` + `bound_subject` on roles; `ClusterSecretStore` with `namespace` and `conditions`; docs drift fixed; region literal removed. Its live proof: a second `apply --apply` on the scratch vault reports every op already current; a hand-edited policy is shown as a diff and repaired.

### Task 2: Namespaces and per-app identities from config

**Files:** `src/ladon/apply/plan.py` (+ new ops), `src/ladon/apply/client.py`, `src/ladon/config/{teams,policies,auth,eso}.py`, new `src/ladon/config/apps.py`, `src/ladon/apply/cli.py`, tests, `docs/baseline.md`, `config/example.yaml`.

- New op `ensure-namespace` (`sys/namespaces/<name>`, read-before-write, verified). `root-config` creates every namespace in `teams:`, and inside each: the KV v2 mount named by `deployment.kv_mount`, `team-readwrite`, `team-readonly`, `external-secrets-read`. All existing namespace-scoped ops take an explicit namespace and set the `X-Vault-Namespace` header through the client (verify the client supports it; add it if not).
- `team-onboarding` runs inside the team's namespace (mount `jwt-<cluster>` there), with `bound_issuer`, the ESO namespace parameter, and the pinned `bound_subject`.
- New phase `app-onboarding` (`--team <t> --app <a> --cluster <c> [--k8s-namespace <ns>] [--eso-namespace <ns>]`) rendering, per D17: policy `eso-<app>-readonly` (read on `kv/data/apps/<app>/*` and `kv/data/apps/<app>`, read+list on `kv/metadata/apps/<app>/*`, plus `auth/token/{lookup,renew,revoke}-self` and `sys/capabilities-self`), role `eso-<team>-<app>` on `jwt-<cluster>` (`bound_subject=system:serviceaccount:<eso-ns>:eso-<team>-<app>`, `token_policies=[eso-<app>-readonly]`, `token_no_default_policy=true`), and the Kubernetes manifests: a `ServiceAccount eso-<team>-<app>` in the ESO namespace, a `ClusterSecretStore ladon-<team>-<app>` (`namespace: <team>`, jwt path `jwt-<cluster>`, role `eso-<team>-<app>`, selector `kubernetes.io/metadata.name: <k8s-namespace>`), rendered as documents the apply layer of Task 3 applies. `plan` prints both halves. Per D18a the service account lives in Ladon's own namespace (`--sa-namespace`, default `ladon`), so `bound_subject=system:serviceaccount:<sa-namespace>:eso-<team>-<app>` and the store's `serviceAccountRef` carries that namespace; `--eso-namespace` is only where the operator runs.
- Tests: plan snapshots for each phase; the policy string is asserted exactly; a fake-client convergence test for `ensure-namespace`.
- Live proof: `apply --phase root-config --apply` creates namespace `platform`; `apply --phase team-onboarding --team platform --cluster <c> --apply`; `plan --phase app-onboarding` renders for a sample app.

### Task 3: Apply manifests to the cluster, install ESO from code, live round-trip

**Files:** new `src/ladon/install/k8s.py` (`apply_documents(docs, *, field_manager="ladon", dry_run: bool)` via `kubectl apply --server-side --field-manager --force-conflicts=false -f -`, `--dry-run=server` for the diff; read-back with `kubectl get -o json` and a comparison of the fields we set; API-version probe for ESO CRDs `v1` vs `v1beta1` via `kubectl api-resources`), `src/ladon/install/eso.py` (use the probe; keep pinned chart), `src/ladon/apply/cli.py` (`app-onboarding --apply` applies the OpenBao half then the cluster half), tests with a fake runner.

- `install_eso` first detects an existing operator (`kubectl get deploy -A -l app.kubernetes.io/name=external-secrets`, or the CRD) and, when found, reports its version and skips the install (D18a). Live proof on the scratch vault + dev cluster: the existing operator is detected and reused; onboard a sample app whose Kubernetes namespace is `ladon-sample`; write `kv/apps/sample/demo` (through the CLI's existing MCP write function or `bao kv put` in the pod, values are throwaway); apply an `ExternalSecret`; the Kubernetes Secret appears with the expected keys. Negative: the same `ExternalSecret` in namespace `ladon-other` referencing `ladon-platform-sample` must fail. Record both outcomes in the CHANGELOG.

### Task 4: The backend

**Files:** new `src/ladon/web/{app,identity,groups,routes_secrets,routes_admin,jobs}.py`, `src/ladon/mcp/openbao.py` (reuse `OpenBaoSecrets` as the service layer), `src/ladon/datastore/*` (+ a `jobs` table and migration), `src/ladon/config/loader.py` (`identity.header`, `groups`, `admins`, `service_callers`), `pyproject.toml` + `uv.lock` (fastapi, uvicorn, python-multipart if needed; pinned), `docs/versions.md`, `ui/` build wired to be served from `LADON_UI_DIST`.

- Identity: `identity.header` (default `x-goog-authenticated-user-email`, value may carry an `accounts.google.com:` prefix; strip it), required on every `/api` route except `/healthz`; `GroupSource` Protocol with `ConfigGroupSource` (email and group names to namespaces; `admins`). `/api/me` returns `{email, namespaces, is_admin}`.
- The UI's routes, exactly as `ui/src/lib/api.ts` calls them: namespaces, secrets list/detail (masked), create, update (merge semantics: `data` merges, `delete_keys` removes, both cannot name the same key, an empty result is refused), reveal (behind the `RevealGate`; deployed default `DenyGate`), namespace requests (create, list mine; admin list/approve/reject; approve creates the namespace through the apply layer), allow-list admin. Every write records an audit row through the existing `ToolCallLog` (rename to a neutral `audit_log` only if the migration is trivial; otherwise reuse).
- Migrations run at boot (`alembic upgrade head` from the entrypoint, like the sibling; a failed upgrade exits non-zero). `LADON_DATABASE_URL` may be SQLite for dev (`sqlite:////data/ladon.db`) and Postgres for anything shared; document both.
- Tests: TestClient with an injected fake `OpenBaoSecrets` and SQLite; one test per route incl. the identity-missing 401, non-member 403, admin-only 403; merge semantics; reveal denied by default.
- Live proof: run the backend locally against the scratch vault (port-forward) with a config mapping the operator's email to `platform`; the UI (`vite` proxy) lists the namespace and the sample secret's key names.

### Task 5: Apply operations as endpoints with background jobs

**Files:** `src/ladon/web/routes_admin.py`, `src/ladon/web/jobs.py`, tests.

- `POST /api/admin/apply/{root-config|team-onboarding|app-onboarding|eso-install}` (admin only) with the same parameters the CLI takes, returning a job id; `GET /api/admin/jobs/{id}` returns state (`queued|running|done|failed`), server-computed elapsed time, and the op-by-op report (`create|update|already current`, never a value). Jobs run in a thread with their own OpenBao client; one job per kind at a time (a second request while one runs answers 409 with the running id). Audit row per job.
- Live proof: onboard the sample app through the endpoint; second call reports every op already current.

### Task 6: Image, deploy contract, and the platform deploy

**Files:** new `Dockerfile` (python:3.14-slim by digest; `helm` and `kubectl` pinned by version and sha256; non-root; `alembic upgrade head` then `uvicorn` in an entrypoint), `.github/workflows/image.yml` (build on `main`, push to a registry named by repository variables, workload identity federation with variables `LADON_IMAGE`, `GCP_WIF_PROVIDER`, `GCP_BUILD_SA`, the same shape the sibling platforms use), new `deploy/ladon/` (`gojira.yaml` class `web`, `k8s/base/` with `ServiceAccount ladon`, a namespaced `Role` on secrets/configmaps and a `ClusterRole` limited to ESO CRDs + namespaces + serviceaccounts in the ESO namespace, `RoleBinding`s, a `PersistentVolumeClaim` for the SQLite dev datastore, an `ExternalSecret`-free bootstrap `Secret` reference for `LADON_DATABASE_URL`/config), `docs/deployment-order.md` (a "platform cluster, single node" path), README status.
- The deploy platform's contract: `gojira.yaml` with `app: ladon`, `class: web`, `service_account: ladon`, `data_mount` for the PVC, `config_from`/`secrets_from` for the config and bootstrap Secret. Manifests preserved by the platform's generator must be in `k8s/base` and registered in its kustomization.
- Live proof: image built (locally with `docker build` if the workflow's identity is not yet granted; the workflow is the durable path), pushed, and the backend running in namespace `ladon` on the platform cluster next to the vault (moved out of `ladon-scratch` into `ladon`), reachable through a port-forward, `/healthz` 200, `/api/me` correct for the operator's email.

### Task 7: The acceptance matrix, live

Not a code task. Using the deployed backend: onboard a throwaway app through the endpoint; then (a) a token from its ESO role reading another app's prefix gets 403; (b) an `ExternalSecret` in another namespace referencing its store is rejected; (c) a JWT for a different service account against its role fails with an invalid-subject error; (d) a second onboard is a no-op. Record all four in the CHANGELOG. Anything that fails becomes a fix task before Task 8.

### Task 8: The role audit

**Files:** new `src/ladon/apply/audit.py` (`audit_roles(client, namespace)`: every JWT role's `token_policies`, `token_no_default_policy`, `bound_subject`; the `default` policy's capabilities on `kv/*` and `sys/policies/*`; a finding per deviation), CLI `audit --namespace`, endpoint `GET /api/admin/audit/roles`, tests, docs.
- Live proof: clean on the platform vault; a deliberately widened `default` policy is reported.

### Task 9: The machine lane and the first consumer

**Files (ladon):** `src/ladon/web/service_lane.py` (`POST /api/service/secrets`: OIDC ID token verified against `service_callers[].issuer` and `audience`, subject allow-listed, `acting_email` required, namespace pinned per subject; same merge semantics; audit row), config, tests with a locally signed token and a JWKS fixture.
**Files (assembly room, separate repo, its own PR):** a `LadonClient` (`put_secret(namespace, path, data)` + `ensure_external_secret(k8s_namespace, name, path)`) used by the colleague-secret and Slack-secret delivery paths when `ladon_url` is configured, else the existing direct Secret write; the config-token store is NOT moved. Flag off by default.
- Live proof: from the room's pod identity, one app's `-cabal` secret written through the lane; the pod's Secret is the ESO-managed one with the same name; the pod starts.

### Task 10: Docs to truth

README status section rewritten to what is proven live; ROADMAP checkboxes ticked only for proven items; `docs/deployment-order.md` gains the platform-cluster path; CHANGELOG consolidated.

---

## Order and gates

1 → 2 → 3 (gate: the ESO round-trip and its negative) → 4 → 5 → 6 (gate: backend running on the platform cluster) → 7 (gate: all four negatives) → 8 → 9 → 10.
