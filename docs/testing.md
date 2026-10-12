# Testing

Sero's tests are small, fast, and meaningful. Each test earns its place by
catching a real failure mode. We do not chase a coverage number, and we do not
write tests that only restate the implementation.

This document describes the testing philosophy and the layers, mapped to the
stages a change passes through on its way to a deployed cluster. Tooling
specifics are filled in as the relevant code lands; the philosophy here is
stable.

## Principles

- **Small and fast.** A test should run in milliseconds to low seconds. Slow,
  broad tests get run less and trusted less.
- **Meaningful.** Test behavior and contracts, not internals. A test that breaks
  only because you renamed a private function is noise.
- **No coverage theater.** We do not add tests to move a percentage. Coverage is
  a diagnostic for finding untested risk, not a target.
- **Secrets-safe by construction.** Tests never use real secret values and never
  print secret material. Fixtures use obvious fake values that the secrets-hygiene
  check recognizes as non-secret. A test that needs a "secret" uses a clearly
  marked dummy.
- **Deterministic.** No reliance on wall-clock time, network flakiness, or
  ordering. The gated integration layer is the only place real infrastructure is
  touched, and it is explicitly opt-in.

## The layers

The layers map to deployment stages, fastest and cheapest first. Earlier layers
run on every push; the gated layer runs only when explicitly invoked.

### 1. Static and lint (every PR, seconds)

Runs with no build and no infrastructure. This is the floor that CI enforces on
every pull request.

- YAML, Helm, and Terraform validate and format checks (`helm lint`,
  `helm template` parses, `terraform validate`, `terraform fmt -check`).
- The conventions lint: em dashes in prose (warns, never fails), no floating version pins, no
  company-specific identifiers, and no cloud region literals. See
  [../scripts/lint-conventions.sh](../scripts/lint-conventions.sh).
- A secrets-material check: scan the diff and tree for anything that looks like a
  committed key, token, or credential, and fail if found. This is the real gate;
  the `.gitignore` is only a backstop.

### 2. Unit (every PR, seconds)

Pure, in-process tests of logic with no external dependencies.

- Policy and role rendering: given inputs, the generated OpenBao policy or role
  is exactly what we expect.
- Config parsing and validation: well-formed config parses; malformed config is
  rejected with a clear error; placeholder or empty secret values are refused.

### 3. Contract (every PR, seconds to low tens of seconds)

Tests that rendered artifacts match the opinionated defaults, without deploying
anything. These catch drift between what we intend to ship and what we actually
render.

- Rendered Helm values match the opinionated defaults for each environment.
- Rendered OpenBao policies match their golden expectations.
- ESO `ExternalSecret` and `SecretStore` resources render correctly for each
  supported target (Kubernetes, GCP Secret Manager, AWS Secrets Manager).

Golden-file comparisons are fine here, as long as the golden files are reviewed
artifacts and not just snapshots of whatever the code happened to produce.

### 4. Gated integration smoke (opt-in, minutes)

The one layer that touches real infrastructure. It is gated: it does not run on
every PR, only when explicitly invoked (a labeled job or a manual workflow
dispatch) against a disposable environment.

The canonical smoke test is a single secret round-trip:

1. Write a dummy secret to OpenBao at a known path.
2. Let ESO sync it to a target store.
3. Read it back from that target store and assert it matches.
4. Clean up: remove the dummy secret and synced copy.

The value used is an obvious dummy, never a real secret. The test asserts the
mechanism works end to end; it does not exercise real credentials.

## What we do not do

- We do not write tests whose only purpose is to raise a coverage number.
- We do not assert on private implementation details that have no externally
  observable contract.
- We do not put real secrets, real project or account IDs, or any
  company-specific identifiers in fixtures. See
  [../CONTRIBUTING.md](../CONTRIBUTING.md).
- We do not run the gated integration layer against shared or production
  infrastructure.

## How this maps to CI

The static, unit, and contract layers run on every pull request and must be
green before merge (part of the [definition of done](../CONTRIBUTING.md#definition-of-done)).
The gated integration layer runs on demand against a disposable environment. The
CI skeleton starts with the static layer and grows to add the others as the code
they test lands.
