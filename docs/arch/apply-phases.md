---
card: arch-card:ladon-apply-phases
updated: 2026-09-28
anchor: src/ladon/apply/cli.py sha=a9cecc2512c7f3449a47dd407cd9ba01b4c785e4
anchor: src/ladon/apply/executor.py sha=18922053cde23e70de3d78580d36f6d82d61b14a
anchor: src/ladon/apply/plan.py sha=03fb62a3b7ed00b58b7c06bd42ebf9a7e7e42f42
---
# apply-phases — render → plan → diff → write → verify, OpenBao then cluster

WHAT / SEAM: `run_phase(args, *, config, apply, address) -> PhaseRun` (apply/cli.py:665) is the ONE entry both the CLI and the backend job runner (web/routes_admin.py:_work, web/service_lane.py `ClusterSync.onboard`) use. Phases: root-config, team-onboarding, app-onboarding, sync. Plan = ordered `WriteOp(kind, path, data, namespace)` list (apply/plan.py); executor `apply_plan(plan, client, apply) -> ApplyResult` (apply/executor.py:622).
OPS / FLOW: `_build` renders BOTH halves (plan + cluster documents) before any write → `_require_eso()` probes the operator/CRD version → connect (BaoConnection.from_env + TokenAuth.from_env) → executor diffs EVERY op against live (create | update | already current | blocked) → if apply and nothing blocked: write each needed op, read back, verify → if OpenBao half blocked, stop → converge cluster documents (install/k8s.py). `PhaseRun.error` carries a post-write cluster failure alongside the report.
INVARIANTS:
- Dry-run default; the dry-run still connects and reads so the diff is real. `plan` subcommand never connects.
- Whole plan diffed before any write; any blocked op → nothing written. WHY: no half-applied plans.
- Immutable fields (engine type, KV version) → blocked, never destroyed-and-recreated. WHY: changing them moves/destroys data; operator decision.
- Each write read back and verified before next op. WHY: a policy that silently fails to land is worse than a loud failure.
- OpenBao before cluster; blocked OpenBao half → cluster half not applied. WHY: a store applied against a role that does not exist fails later, elsewhere, at ESO refresh.
- Namespace is part of the op, not the connection (`scoped_namespace`); one client serves many namespaces.
- Op summaries = kind + path only; payloads never printed or logged.
- Jobs build their own client from env; nothing from the HTTP request reaches the thread (web/jobs.py). One job per kind at a time.
- KV mounted with version pinned "2" (plan.py:KV_VERSION).
CONSUMERS: apply/cli.py main (`scripts/ladon plan|apply`), web/routes_admin.py `POST /api/admin/apply/{phase}` (+ eso-install), web/service_lane.py sync implicit onboarding. Inputs from config/ renderers (teams.py, policies.py, auth.py, apps.py).
API: run_phase, run_eso_install, build_plan, apply_plan, supported_kinds; CLI flags in cli.py:_add_plan_flags.
