---
card: arch-card:sero-machine-lane
updated: 2026-10-15
anchor: src/sero/web/service_lane.py sha=f4ec281969d63d71e1af15401503049049d778c1
anchor: src/sero/web/settings.py sha=cd102cd7cc660d9be579b36d303ff3f6cb5b846c
---
# machine-lane: /api/service/*, a provisioner writing on a person's behalf (D16 in docs/design.md)

WHAT / SEAM: FastAPI router in web/service_lane.py, mounted under /api (web/app.py). Auth = OIDC ID token (`Authorization: Bearer`), verified per configured `ServiceCaller` (web/settings.py:101; parsed by parse_service_callers). `ServiceLane.authenticate` → caller; `ServiceLane.allow` → per-caller `TokenBucket` (separate read and write budgets). `ClusterSync` dataclass holds the cluster collaborators (converge/remove/read/list_kind/onboard=apply.cli.run_phase) so tests inject a fake cluster.
API (all bodies carry required `acting_email`; writes are dry-run unless `apply: true`):
- GET /service/secrets (query): key names, version, updated_at. Read budget.
- POST /service/secrets: merge keys into one path under `apps/`.
- DELETE /service/secrets: remove the path and all versions.
- POST /service/sync: converge one ExternalSecret against the app's store; onboards the app (policy, role, SA, ClusterSecretStore via run_phase) when the store is absent (#76).
- DELETE /service/sync: remove one ExternalSecret (and, via creationPolicy Owner, its k8s Secret).
- DELETE /service/app: offboard (store, SA, JWT role, policy) (#78). 409 while any ExternalSecret in the one requested Kubernetes namespace still reads the store (the check lists that namespace only, not the cluster); 503 if `deployment.cluster` unset.
OPS / FLOW: bearer → unverified iss/aud pick the caller → AssertionVerifier (web/assertion.py) re-verifies sig/iss/aud/exp → `sub` must equal pinned subject → iat age ≤ MAX_SERVICE_TOKEN_AGE_SECONDS (900) → bucket → `_require_pinned(namespace)` → path/cluster-target checks → put goes through mcp.server `create_secret_write` / `update_secret_write`; read, delete and offboard call the secrets port directly; sync, unsync and offboard use ClusterSync → audit row filed under acting_email with `lane=service`, `via=<caller>`.
INVARIANTS:
- Caller's `audience` must differ from the human assertion audience (refused at config load). WHY: else a person's browser token replays as a machine credential.
- jwks_url must be https; all of name/issuer/jwks_url/audience/subject/namespaces required; `acting_email_required: false` refused. WHY: each field is a boundary.
- Authorization rests ONLY on subject + pinned namespaces; acting_email is recorded, never trusted.
- No plain identity header, no dev hatch on this lane; zero configured callers → every call 401.
- Paths confined to `apps/` (and `apps/<app>/` for sync). Store name derived by config/apps.py, same function onboarding uses: never taken from the body.
- Existing cluster objects must carry field manager `sero` (install/k8s.py:require_managed) before sync converges or unsync/offboard deletes (409 if foreign, 403 if another app's). WHY: SSA under the same manager name silently takes fields; the ownership check is what makes the delete gate mean anything.
- Every 401/429 is audited before answering.
- Budgets are in-process: a second replica gets a second bucket (by design).
CONSUMERS: the room (its client lives in its own repo, not checked here). Changing a route, body field, or status code breaks the room. Tests: tests/test_web_service_lane.py.
STALE IN CODE: the unsync_secret docstring still says off-boarding is operator-only / not on the machine lane; #78 added DELETE /service/app. Trust the routes. (The module docstring was fixed by #92.)
