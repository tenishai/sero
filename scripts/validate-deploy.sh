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
# One directory is deliberately outside both halves: standalone/front-door/.
# Neither the kubectl glob below nor "kubectl apply -f <dir>" recurses, so
# nothing in it is read, and that is on purpose rather than by accident. Its
# manifests are cloud-provider custom resources whose definitions are not on
# every cluster, and they carry angle-bracket placeholders for a hostname and
# an address name, which are not valid values for the fields they sit in. Both
# would fail the full mode's schema validation on a perfectly good file. The
# front door is applied by hand, once, against the one cluster that has it.
#
# It deploys nothing. Like scripts/validate-helm.sh, this is a contract test:
# it proves the manifests are well-formed and complete, not that they are
# applied anywhere.
#
# Two modes, because field-level validation is not an offline operation.
# "kubectl apply --dry-run=client" validates every field against the API
# server's schema, which means it needs an API server to ask; with no cluster
# reachable it fails on the schema download, not on the manifests. So:
#
#   * With a reachable cluster (an operator's laptop), it runs exactly:
#         kubectl apply --dry-run=client -k deploy/ladon/k8s/base
#         kubectl apply --dry-run=client -f deploy/ladon/standalone/
#     which is full schema validation and the real gate.
#
#   * With no cluster (CI), it falls back to what can be checked locally, and
#     that is a short list: the YAML parses, the kustomization resolves every
#     resource it names, and each document decodes to a group/version/kind
#     kubectl recognizes. In practice this catches malformed YAML and a
#     resource left out of the kustomization. It does NOT catch a misspelled
#     field, a wrong field type, or an invalid value: those reach the API
#     server's schema and nothing else. The mode that ran is printed, so a
#     green run never overstates what it proved.
#
# The inventory check below runs in both modes and is what makes the offline
# mode worth something: it asserts the built set contains exactly the objects
# this deployment needs, by kind and name. A file dropped from the
# kustomization, or renamed, fails here rather than at apply time on a cluster.
#
# Set LADON_KUBE_CONTEXT to choose a context; otherwise the current one is used.
# Requires kubectl on the path (pinned version in docs/versions.md).

set -euo pipefail

BASE_DIR="deploy/ladon/k8s/base"
STANDALONE_DIR="deploy/ladon/standalone"

# Every object each half must produce, as "<kind>/<name>". Kept here rather
# than derived from the files, so deleting a file fails the check instead of
# quietly shrinking what it expects.
BASE_EXPECTED="
ServiceAccount/ladon
Role/ladon
RoleBinding/ladon
ClusterRole/ladon
ClusterRoleBinding/ladon
PersistentVolumeClaim/ladon-data
NetworkPolicy/ladon-default-deny
NetworkPolicy/ladon-backend
NetworkPolicy/ladon-openbao
"
STANDALONE_EXPECTED="
Deployment/ladon
Service/ladon
"

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

inventory() {
  # inventory <manifest-file>  ->  "<kind>/<name>" per line, sorted.
  #
  # "label --local" never contacts a server; it is used purely as a decoder,
  # and the label it sets is discarded with the output. The template reads the
  # two fields straight off each decoded object, so a RoleBinding's roleRef
  # cannot be mistaken for the Role itself.
  kubectl label --local -f "$1" \
    -o go-template='{{.kind}}/{{.metadata.name}}{{"\n"}}' \
    ladon.dev/validate=1 2>/dev/null \
    | sort -u
}

check_inventory() {
  # check_inventory <label> <manifest-file> <expected-list>
  local label="$1" manifest="$2" expected="$3"
  echo -n "check: ${label} ... "
  local actual missing=""
  actual="$(inventory "$manifest")"
  local want
  while IFS= read -r want; do
    [[ -z "$want" ]] && continue
    if ! printf '%s\n' "$actual" | grep -qxF "$want"; then
      missing+="  missing: ${want}"$'\n'
    fi
  done <<< "$expected"
  if [[ -n "$missing" ]]; then
    echo "FAILED"
    printf '%s' "$missing"
    echo "  found:"
    printf '%s\n' "$actual" | sed 's/^/    /'
    return 1
  fi
  echo "ok"
  return 0
}

if cluster_reachable; then
  echo "mode: full (schema validated against a reachable cluster)"
  run "kustomize base" kctl apply --dry-run=client -k "$BASE_DIR" || fail=1
  run "standalone"     kctl apply --dry-run=client -f "$STANDALONE_DIR/" || fail=1
else
  echo "mode: offline (no cluster reachable; malformed YAML and a missing"
  echo "      resource are caught, field-level schema is not)"
  run "kustomize base builds" kubectl kustomize "$BASE_DIR" || fail=1
fi

# The inventory check runs in both modes: the full mode proves every field is
# valid, but not that every object is present.
built="$(mktemp)"
standalone_all="$(mktemp)"
trap 'rm -f "$built" "$standalone_all"' EXIT

if kubectl kustomize "$BASE_DIR" >"$built" 2>/dev/null; then
  check_inventory "base inventory" "$built" "$BASE_EXPECTED" || fail=1
else
  echo "check: base inventory ... FAILED (kustomize build failed)"
  fail=1
fi

: >"$standalone_all"
for manifest in "$STANDALONE_DIR"/*.yaml; do
  echo "---" >>"$standalone_all"
  cat "$manifest" >>"$standalone_all"
done
check_inventory "standalone inventory" "$standalone_all" "$STANDALONE_EXPECTED" || fail=1

if [[ $fail -ne 0 ]]; then
  echo "deploy validation failed"
  exit 1
fi
echo "deploy validation passed"
