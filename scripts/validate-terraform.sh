#!/usr/bin/env bash
#
# validate-terraform.sh - static checks for the Terraform substrate layer (D10).
#
# Runs the credential-free part of the Terraform pipeline: a recursive format
# check, then `init -backend=false` + `validate` for each module under
# terraform/. It does NOT run `plan` or `apply`: those need cloud credentials and
# a state backend, and applying is agent-driven and dry-run-first (design.md D3),
# not a CI step. See docs/deployment-order.md ("Terraform apply flow") for the
# target apply-before-merge process this check feeds into.
#
# Run locally or in CI. Requires terraform on the path (pinned version in
# docs/versions.md).

set -euo pipefail

TF_ROOT="terraform"

if [[ ! -d "$TF_ROOT" ]]; then
  echo "no ${TF_ROOT}/ directory; nothing to validate"
  exit 0
fi

echo "terraform fmt check (recursive)"
terraform fmt -check -recursive "$TF_ROOT"

# Validate every directory that declares Terraform and is not a .terraform cache.
# A module is any directory directly containing one or more .tf files. Use a
# portable read loop rather than mapfile (not present in older bash).
module_dirs=()
while IFS= read -r dir; do
  module_dirs+=("$dir")
done < <(
  find "$TF_ROOT" -type f -name '*.tf' -not -path '*/.terraform/*' \
    -exec dirname {} \; | sort -u
)

if [[ ${#module_dirs[@]} -eq 0 ]]; then
  echo "no Terraform modules found under ${TF_ROOT}/"
  exit 0
fi

fail=0
for dir in "${module_dirs[@]}"; do
  echo -n "validate: ${dir} ... "
  if terraform -chdir="$dir" init -backend=false -input=false >/dev/null 2>&1 &&
    terraform -chdir="$dir" validate >/dev/null; then
    echo "ok"
  else
    echo "FAILED"
    # Re-run validate to surface the error output for the log.
    terraform -chdir="$dir" validate || true
    fail=1
  fi
done

if [[ $fail -ne 0 ]]; then
  echo "terraform validation failed"
  exit 1
fi
echo "terraform validation passed (${#module_dirs[@]} module(s))"
