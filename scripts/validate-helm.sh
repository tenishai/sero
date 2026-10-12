#!/usr/bin/env bash
#
# validate-helm.sh - contract check for the OpenBao install layer.
#
# Renders the pinned OpenBao chart with Sero's values to prove they are valid
# against that chart version. This is the contract test described in
# docs/testing.md for the install layer: it does not deploy anything.
#
# Run locally or in CI. Requires helm on the path (pinned version in
# docs/versions.md).

set -euo pipefail

# Pinned chart version. Keep in sync with docs/versions.md; bumps are their own
# reviewed PR.
CHART_VERSION="0.28.4"
CHART="openbao/openbao"
REPO_URL="https://openbao.github.io/openbao-helm"
VALUES_DIR="deploy/openbao/values"

echo "Adding pinned chart repo"
helm repo add openbao "$REPO_URL" >/dev/null
helm repo update openbao >/dev/null

render() {
  # render <label> <values-args...>
  local label="$1"; shift
  echo -n "render: ${label} ... "
  if helm template sero-openbao "$CHART" --version "$CHART_VERSION" "$@" >/dev/null; then
    echo "ok"
  else
    echo "FAILED"
    return 1
  fi
}

fail=0
render "base"        -f "$VALUES_DIR/values-base.yaml" || fail=1
render "base + dev"  -f "$VALUES_DIR/values-base.yaml" -f "$VALUES_DIR/values-dev.yaml" || fail=1

if [[ $fail -ne 0 ]]; then
  echo "helm validation failed"
  exit 1
fi
echo "helm validation passed (chart ${CHART_VERSION})"
