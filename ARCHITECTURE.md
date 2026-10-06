---
card: repo-card:ladon
updated: 2026-10-05
anchor: pyproject.toml sha=1059e4b3a546c630cf54381ac418d75a55667544
anchor: src/ladon/web/app.py sha=52e5d37ae1873a15455663273d6815ee9132754f
---
# ladon: repo card

WHAT: secrets/vault control plane on OpenBao. Renders + converges OpenBao config (namespaces, policies, JWT/OIDC auth, KV v2) and per-app ESO identities on the cluster it runs on; serves an HTTP backend + Svelte UI for people and a machine lane for provisioners. Never stores secret values (datastore holds audit/jobs/requests/tokens only).
STATE: main, pyproject `version = "0.1.0"` (optional `postgres` extra = psycopg, 2026-10-05) (bumped 2026-10-05 for the go-public tag), no git tags yet, CHANGELOG all under [Unreleased]. Dev-shape deployment proven live per README Status (unverified here). Active: machine lane (read/delete/unsync #74, implicit onboarding on sync #76, `DELETE /api/service/app` offboard #78), k8s managed-fields reads (#77), front door + NetworkPolicy (#72), KMS seal overlay (#71).
STACK: Python >=3.14, uv (`package = false`: tooling, not a distributable; no console script). FastAPI+uvicorn, hvac (OpenBao), SQLAlchemy+Alembic, pyjwt[crypto], mcp (FastMCP), PyYAML: all pinned exact. Shells out to `helm` and `kubectl` (pinned + checksummed in Dockerfile). UI: Svelte 5 + Vite in ui/, built into the image at /srv/ladon/ui. Image: Dockerfile, digest-pinned bases, non-root uid 10001. The read-only root filesystem is set by the deployment manifest (deploy/ladon/standalone/deployment.yaml), not by the image.
ENTRY:
- CLI `scripts/ladon` → `python -m ladon` → src/ladon/apply/cli.py:main. Subcommands plan | apply (dry-run unless --apply) | install-eso | seal-values | audit; `--phase` ∈ root-config, team-onboarding, app-onboarding, sync (cli.py:234).
- Backend `python -m ladon.web` → web/boot.py:main (alembic upgrade head, then uvicorn on `ladon.web.app:build_default_app`). Image ENTRYPOINT is this.
- Routes: /healthz (unauth); /api/* human lane (web/routes_secrets.py); /api/admin/* (web/routes_admin.py: namespace-request approve/reject, allow-list, `POST /apply/{phase}` → background job); /api/service/* machine lane (web/service_lane.py); `/{path}` serves the UI.
- MCP: mcp/server.py:build_server is built and tested, but NO entrypoint serves it (no caller in src/). README Status: "no OAuth 2.1 server / MCP mount over HTTP".
SEAMS → card:
- secret-ports   src/ladon/mcp/ports.py          (SecretReader/Writer/Eraser; reads masked, writes return key names) → docs/arch/secret-ports.md
- machine-lane   src/ladon/web/service_lane.py   (OIDC-token API for provisioners; the room calls it) → docs/arch/machine-lane.md
- apply-phases   src/ladon/apply/cli.py:run_phase + executor.py (render→plan→diff→write→verify) → docs/arch/apply-phases.md
- cluster-apply  src/ladon/install/k8s.py        (SSA under field manager `ladon`, ownership gate) → docs/arch/cluster-apply.md
- caller-identity src/ladon/web/identity.py + assertion.py (proxy header vs signed assertion)     → docs/arch/caller-identity.md
- (uncarded) config/, pure renderers: teams.py `Team`, policies.py, auth.py, apps.py (D17 in docs/specs/2026-09-22-platform-vault-design.md: per-app identity naming `eso-<team>-<app>-<digest>` / `ladon-<team>-<app>-<digest>`; MUST stay stable, offboard re-derives names from it), eso.py, seal.py/seal_values.py (KMS auto-unseal overlay), loader.py `DeploymentConfig`.
- (uncarded) datastore/: models.py tables tool_call_log, mcp_refresh_tokens, mcp_allow_list, namespace_requests, jobs, reveal_flows, reveal_grants; repositories.py; engine.py reads LADON_DATABASE_URL (unset → datastore None). migrations/.
- (uncarded) audit.py `AuditRecorder`: failure rows in their own transaction so a rollback can't erase them; shared by web + MCP (web/audit.py re-exports).
- (uncarded) hygiene.py: mask, redact, placeholder refusal, `WriteRefused` base; web/jobs.py JobRunner (one job per kind, daemon threads, `recover_interrupted` at boot); web/reveal.py RevealGate (DenyGate default; step-up not built); apply/audit.py (over-broad role/policy finder, exit 3 on serious).
DEPS: no Python imports of sibling repos. Called by the room, over /api/service/{secrets,sync,app} (the caller's code lives in its own repo, not checked here). deploy/ladon/gojira.yaml = deploy contract for gojira (class `gateway`). README names liminal as the intended MCP front: aspirational, nothing wires it.
HEALTH: tests/test_*.py, `uv run pytest` (fakes: tests/fake_openbao.py, fake_cluster.py, fake_secrets.py, fake_assertion.py; no live OpenBao/cluster in tests). UI: Node's built-in test runner (`npm test` runs `node --experimental-strip-types --test`) + Playwright smoke. CI .github/workflows/ci.yml on PR + push main: conventions lint (scripts/lint-conventions.sh), helm render + offline kustomize validate, terraform validate, ruff check + format --check + pytest, docker build + /healthz smoke, UI build/test/e2e. No image build in CI: images are built by hand from a clean export of a commit and tagged with its short sha (see README). Runners from `vars.CI_RUNS_ON`; the image smoke job reads `vars.CI_BUILD_RUNS_ON`. No branch protection: CI reports, never blocks.
GOTCHAS:
- ladon is SECRETS, NOT observability (that is the observability platform).
- Datastore "PostgreSQL" (docs/design.md D11) is not installable from the lock: uv.lock has no psycopg/any PG driver. Every shipped deploy uses `sqlite:////data/ladon.db` on a RWO PVC → one replica, one process (gojira.yaml `class: gateway`).
- Most write surfaces are dry-run by default: CLI `--apply`, API body `apply: true`. NOT all: approving a namespace request builds `ApplyBody(teams=[...], apply=True)` and runs a real root-config apply with no dry-run option (web/routes_admin.py), and the allow-list POST and DELETE write immediately.
- One deployment = one OpenBao = one environment (`deployment.environment`); `env` on a request is a label that is checked, never a selector (mcp/server.py:require_environment).
- OpenBao token comes only from env (BAO_TOKEN/VAULT_TOKEN via apply/client.py TokenAuth.from_env), never config. Config YAML via LADON_CONFIG (path) or LADON_CONFIG_YAML (inline); gojira path must NOT set LADON_CONFIG.
- mcp/server.py `*_impl`/`*_write` functions are the shared write logic for ALL surfaces (web routes and machine lane import them); "mcp" in the path does not mean MCP-only.
RELATED DOCS: docs/design.md (D1 to D12 decisions), docs/backend.md (env, identity trust boundary), docs/deployment-order.md, docs/specs/2026-09-22-platform-vault-design.md (D13 to D18 decisions), docs/plans/2026-09-22-platform-vault.md, deploy/ladon/README.md, docs/testing.md, docs/versions.md.
