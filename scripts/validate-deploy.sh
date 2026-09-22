#!/usr/bin/env bash
#
# validate-deploy.sh - contract check for the deploy manifests.
#
# Checks both halves of deploy/ladon:
#   * k8s/base       the manifests the deployment platform's generator
#                    preserves, as a kustomization.
#   * standalone/    the Deployment and Service for a cluster where that
#                    platform is not in use.
#
# It deploys nothing. Like scripts/validate-helm.sh, this is a contract test:
# it proves the manifests are well-formed and accepted, not that they are
# applied anywhere.
#
# Two modes, because full validation is not an offline operation. "kubectl
# apply --dry-run=client" validates every field against the API server's
# schema, which means it needs an API server to ask; with no cluster reachable
# it fails on the schema download, not on the manifests. So:
#
#   * With a reachable cluster (an operator's laptop), it runs exactly:
#         kubectl apply --dry-run=client -k deploy/ladon/k8s/base
#         kubectl apply --dry-run=client -f deploy/ladon/standalone/
#     which is full schema validation and the real gate.
#
#   * With no cluster (CI), it falls back to what can be checked locally: the
#     kustomization builds, and every document decodes to a group/version/kind
#     kubectl recognizes. That catches malformed YAML, a resource missing from
#     the kustomization, and a wrong apiVersion. It does not catch a misspelled
#     field. The mode that ran is printed, so a green run never overstates what
#     it proved.
#
# Set LADON_KUBE_CONTEXT to choose a context; otherwise the current one is used.
# Requires kubectl on the path (pinned version in docs/versions.md).

set -euo pipefail

BASE_DIR="deploy/ladon/k8s/base"
STANDALONE_DIR="deploy/ladon/standalone"

# kubectl, with the chosen context if there is one. A function rather than an
# array because bash 3.2 (the default on macOS) rejects an empty array under
# "set -u".
kctl() {
  if [[ -n "${LADON_KUBE_CONTEXT:-}" ]]; then
    kubectl --context "${LADON_KUBE_CONTEXT}" "$@"
  else
    kubectl "$@"
  fi
}

cluster_reachable() {
  kctl cluster-info --request-timeout=10s >/dev/null 2>&1
}

fail=0

run() {
  # run <label> <command...>
  local label="$1"; shift
  echo -n "check: ${label} ... "
  if "$@" >/dev/null 2>&1; then
    echo "ok"
  else
    echo "FAILED"
    echo "--- output ---"
    "$@" 2>&1 | sed 's/^/  /' || true
    echo "--------------"
    return 1
  fi
}

if cluster_reachable; then
  echo "mode: full (schema validated against a reachable cluster)"
  run "kustomize base" kctl apply --dry-run=client -k "$BASE_DIR" || fail=1
  run "standalone"     kctl apply --dry-run=client -f "$STANDALONE_DIR/" || fail=1
else
  echo "mode: offline (no cluster reachable; field-level schema not checked)"
  run "kustomize base builds" kubectl kustomize "$BASE_DIR" || fail=1

  # Decode every document to a known kind. "label --local" never contacts a
  # server; it is used here purely as a decoder, and the label it sets is
  # discarded with the output.
  built="$(mktemp)"
  trap 'rm -f "$built"' EXIT
  if kubectl kustomize "$BASE_DIR" >"$built" 2>/dev/null; then
    run "base documents decode" \
      kubectl label --local -f "$built" -o name ladon.dev/validate=1 || fail=1
  fi
  for manifest in "$STANDALONE_DIR"/*.yaml; do
    run "$(basename "$manifest") decodes" \
      kubectl label --local -f "$manifest" -o name ladon.dev/validate=1 || fail=1
  done
fi

if [[ $fail -ne 0 ]]; then
  echo "deploy validation failed"
  exit 1
fi
echo "deploy validation passed"
