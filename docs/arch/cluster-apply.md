---
card: arch-card:ladon-cluster-apply
updated: 2026-09-28
anchor: src/ladon/install/k8s.py sha=626058838ad0a5b7eea8cf3cc2bb03aedd33a3ef
---
# cluster-apply — kubectl server-side apply under field manager `ladon`

WHAT / SEAM: install/k8s.py wraps `kubectl` via subprocess (`run`, injectable `Runner`). `converge_documents(docs, dry_run)` → `ApplyReport` (per-doc create | update | already current | blocked); `delete_documents` (delete | absent, then gone | terminating | recreated); `read_one`, `list_kind`, `require_managed`, `field_managers`, `eso_api_version`, `detect_eso`. Errors: K8sError, ClusterApplyError(report), ForeignObjectError(WriteRefused → 409), ESONotInstalledError.
OPS / FLOW: _checked(doc) → read live (`kubectl get -o json --show-managed-fields`) → subset-compare (spec, automountServiceAccountToken, labels, annotations; strict under spec.conditions and spec.provider.vault.auth) → only differing docs reach `kubectl apply --server-side --field-manager=ladon` → read back → verify.
INVARIANTS:
- Never `--force-conflicts`. WHY: taking fields from another manager is an operator decision.
- Delete only objects whose managedFields list `ladon` (subresource entries ignored, else ESO's status writes would make ESO "own" everything). Mirror of the no-force rule.
- `--show-managed-fields` on every get (#77). WHY: kubectl strips managedFields by default; without it every Ladon object reads as unowned and every sync/unsync is refused.
- Only `(NotFound)` counts as absent; any other refusal is an error. WHY: "could not look" ≠ "nothing there".
- Namespaced kinds must set a namespace (CLUSTER_SCOPED_KINDS exempt). WHY: else it lands in whatever the kubectl context points at.
- Error text: first line of stderr, ≤ MAX_ERROR_CHARS (200). WHY: kubectl can echo object bodies.
- Ladon manages ONE cluster, its own (in-cluster RBAC: deploy/ladon/k8s/base/{role,clusterrole}.yaml).
CONSUMERS: apply/cli.py run_phase (cluster half), install/eso.py, web/service_lane.py `ClusterSync` defaults. Tests: tests/test_install_k8s.py, tests/fake_cluster.py.
