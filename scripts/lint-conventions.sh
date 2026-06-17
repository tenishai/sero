#!/usr/bin/env bash
#
# lint-conventions.sh - enforce Ladon's hard conventions.
#
# Checks, in order:
#   1. No em dashes (or en dashes) in prose files.
#   2. No company-specific identifiers anywhere in the tree.
#   3. No floating version pins (":latest" images, unpinned GitHub Actions).
#   4. No committed secret material (private keys, credentials).
#
# Runs against tracked files by default (git ls-files). Pass file paths as
# arguments to check only those. Exits non-zero if any check fails.
#
# This is the real gate referenced by docs/testing.md and CONTRIBUTING.md.
# The .gitignore is only a backstop.
#
# Portability: targets both GNU grep (CI, Linux) and BSD grep (macOS). It
# avoids "grep -P" and "\s", and passes file lists to grep directly rather
# than through xargs, whose flags differ across platforms.

set -euo pipefail

SCRIPT_PATH="${BASH_SOURCE[0]#./}"
fail_count=0

# Collect the files to scan: arguments if given, else all tracked files.
# Avoid mapfile/readarray for portability with bash 3.2 (default on macOS).
FILES=()
if [[ $# -gt 0 ]]; then
  FILES=("$@")
else
  while IFS= read -r _f; do
    FILES+=("$_f")
  done < <(git ls-files)
fi

report() {
  # report <check-name>  (hits passed on stdin)
  # Returns non-zero if there are hits. Runs in a pipeline subshell, so it
  # must NOT mutate shell state; the caller accumulates failures from the
  # pipeline's exit status instead.
  local name="$1"
  local hits
  hits="$(cat)"
  if [[ -n "$hits" ]]; then
    echo "FAIL: ${name}"
    echo "${hits}" | sed 's/^/  /'
    return 1
  fi
  echo "ok:   ${name}"
  return 0
}

# Check 1: no em dashes or en dashes in prose -------------------------------
# Em dashes in code (string literals, identifiers) are allowed, so we only
# scan prose file types. Match the literal UTF-8 bytes with grep -F so we do
# not depend on PCRE.
check_dashes() {
  local prose=() f
  for f in "${FILES[@]}"; do
    case "$f" in *.md|*.txt|*.rst) prose+=("$f") ;; esac
  done
  if [[ ${#prose[@]} -eq 0 ]]; then
    echo "ok:   no em or en dashes in prose (no prose files)"
    return 0
  fi
  local em en
  em="$(printf '\xe2\x80\x94')"  # U+2014 em dash
  en="$(printf '\xe2\x80\x93')"  # U+2013 en dash
  { grep -nF -e "$em" -e "$en" "${prose[@]}" 2>/dev/null || true; } \
    | report "no em or en dashes in prose"
}

# Check 2: no company-specific identifiers ----------------------------------
# Ladon is brand-neutral. The denylist token is assembled from fragments so
# this script does not match itself, and the script is excluded from the scan.
check_branding() {
  local a="fu" b="bo"
  local pattern="${a}${b}"   # the company token, never written literally
  local scan=() f
  for f in "${FILES[@]}"; do
    [[ "$f" == "$SCRIPT_PATH" ]] && continue
    scan+=("$f")
  done
  if [[ ${#scan[@]} -eq 0 ]]; then
    echo "ok:   no company-specific identifiers (no files)"
    return 0
  fi
  { grep -niIF "$pattern" "${scan[@]}" 2>/dev/null || true; } \
    | report "no company-specific identifiers"
}

# Check 3: no floating version pins -----------------------------------------
check_pins() {
  local hits="" f

  # 3a. ":latest" container image tags (skip prose and this script).
  local scan=()
  for f in "${FILES[@]}"; do
    case "$f" in *.md|*.txt) continue ;; esac
    [[ "$f" == "$SCRIPT_PATH" ]] && continue
    scan+=("$f")
  done
  if [[ ${#scan[@]} -gt 0 ]]; then
    local latest_hits
    latest_hits="$(grep -nE ':latest([^a-zA-Z0-9._-]|$)' "${scan[@]}" 2>/dev/null || true)"
    [[ -n "$latest_hits" ]] && hits+="${latest_hits}"$'\n'
  fi

  # 3b. GitHub Actions must be pinned to a 40-char commit SHA, not a tag.
  local wf=()
  for f in "${FILES[@]}"; do
    case "$f" in .github/workflows/*.yml|.github/workflows/*.yaml) wf+=("$f") ;; esac
  done
  if [[ ${#wf[@]} -gt 0 ]]; then
    local action_hits
    action_hits="$(grep -nE '^[[:space:]]*-?[[:space:]]*uses:[[:space:]]*[^@]+@' "${wf[@]}" 2>/dev/null \
      | grep -vE '@[0-9a-f]{40}([[:space:]]|$)' || true)"
    [[ -n "$action_hits" ]] && hits+="${action_hits}"$'\n'
  fi

  printf '%s' "$hits" | sed '/^$/d' | report "no floating version pins"
}

# Check 4: no committed secret material -------------------------------------
check_secrets() {
  local scan=() f
  for f in "${FILES[@]}"; do
    [[ "$f" == "$SCRIPT_PATH" ]] && continue
    scan+=("$f")
  done
  if [[ ${#scan[@]} -eq 0 ]]; then
    echo "ok:   no committed secret material (no files)"
    return 0
  fi
  # PEM private key blocks, AWS access key IDs, explicit secret-key assignments.
  local pattern='-----BEGIN [A-Z ]*PRIVATE KEY|AKIA[0-9A-Z]{16}|aws_secret_access_key[[:space:]]*='
  # Use -e so the leading dashes in the PEM marker are not parsed as options.
  { grep -nEI -e "$pattern" "${scan[@]}" 2>/dev/null || true; } \
    | report "no committed secret material"
}

echo "Ladon conventions lint"
echo "----------------------"
check_dashes   || fail_count=$((fail_count + 1))
check_branding || fail_count=$((fail_count + 1))
check_pins     || fail_count=$((fail_count + 1))
check_secrets  || fail_count=$((fail_count + 1))
echo "----------------------"

if [[ $fail_count -gt 0 ]]; then
  echo "conventions lint failed: ${fail_count} check(s) failed"
  exit 1
fi
echo "conventions lint passed"
