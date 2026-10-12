---
card: arch-card:sero-cluster-apply
updated: 2026-10-12
anchor: src/sero/install/k8s.py sha=9f5c2ae12ae4bda9a8c4ac2e7a1a2887f13ce79b
---
# cluster-apply: kubectl server-side apply under field manager `sero`

WHAT / SEAM: install/k8s.py wraps `kubectl` via subprocess (`run`, injectable `Runner`). `converge_documents(docs, dry_run)` → `ApplyReport` (per-doc create | update | already current | blocked); `delete_documents` (delete | absent, then gone | terminating | recreated); `read_one`, `list_kind`, `require_managed`, `field_managers`, `eso_api_version`, `detect_eso`. Errors: K8sError, ClusterApplyError(report), ForeignObjectError(WriteRefused → 409), ESONotInstalledError.
OPS / FLOW: _checked(doc) → read live (`kubectl get -o json --show-managed-fields`) → subset-compare (spec, automountServiceAccountToken, labels, annotations; strict under spec.conditions and spec.provider.vault.auth) → only differing docs reach `kubectl apply --server-side --field-manager=sero` → read back → verify.
INVARIANTS:
- Never `--force-conflicts`. WHY: taking fields from another manager is an operator decision.
- Delete only objects whose managedFields list `sero` (subresource entries ignored, else ESO's status writes would make ESO "own" everything). Mirror of the no-force rule.
- `--show-managed-fields` on every single-object get (`get_argv`, #77); listings (`list_argv`) omit it on purpose, because a listing asks what is there, not who owns it. WHY: kubectl strips managedFields by default; without it every Sero object reads as unowned and every sync/unsync is refused.
- Only `(NotFound)` counts as absent; any other refusal is an error. WHY: "could not look" ≠ "nothing there".
- Namespaced kinds must set a namespace (CLUSTER_SCOPED_KINDS exempt). WHY: else it lands in whatever the kubectl context points at.
- Error text: first line of stderr, ≤ MAX_ERROR_CHARS (200). WHY: kubectl can echo object bodies.
- Sero manages ONE cluster, its own (in-cluster RBAC: k8s/base/role.yaml, k8s/cluster/clusterrole.yaml).
CONSUMERS: apply/cli.py run_phase (cluster half), install/eso.py, web/service_lane.py `ClusterSync` defaults. Tests: tests/test_install_k8s.py, tests/fake_cluster.py.

2026-10-12, the rename: the field manager is `sero`; `require_managed` also accepts `ladon` (`LEGACY_FIELD_MANAGERS`), so objects a pre-rename release applied are still this service's own.
