# Pinned versions

This file is the single source of truth for every version Ladon pins. If Ladon
depends on it, operates it, or runs it in CI, its exact version lives here.

## How to use this file

When you introduce any dependency, tool, chart, provider, container image,
language runtime, or GitHub Action:

1. Web-verify its current latest stable from the authoritative source listed in
   the "Verified against" column. Do not trust memory.
2. Pin to that exact version (a digest where practical for images, a commit SHA
   for GitHub Actions).
3. Add or update its row below, including the date you verified and the source.

Version bumps are their own reviewed PRs. See
[versioning.md](versioning.md) and [../CONTRIBUTING.md](../CONTRIBUTING.md).

The set of pins in this file at a given release tag is exactly the set of
versions that release of Ladon was built and tested against.

## Platform components

Core software Ladon deploys and operates. To be populated during the
architecture phase (see [design.md](design.md)), once each choice is made and
its latest stable is verified.

| Component | Pinned version | Verified against | Date | Notes |
| --- | --- | --- | --- | --- |
| OpenBao | 2.5.4 | openbao.org release notes / github.com/openbao/openbao/releases | 2026-06-17 | Server and CLI. Latest stable at design time; chart pin set when the deploy baseline lands. |
| OpenBao Helm chart | _tbd_ | | | |
| External Secrets Operator | _tbd_ | | | |
| ESO Helm chart | _tbd_ | | | |
| cert-manager | _tbd_ | | | If adopted |

## Infrastructure providers

Terraform providers and similar. Populated as infrastructure code lands.

| Provider | Pinned version | Verified against | Date | Notes |
| --- | --- | --- | --- | --- |
| _none yet_ | | | | |

## Container images

Images Ladon builds on or ships. Pin by digest where practical, not only tag.

| Image | Pinned tag | Digest | Verified against | Date |
| --- | --- | --- | --- | --- |
| _none yet_ | | | | |

## Language runtimes

Languages and their toolchains. Populated once the language decision in
[design.md](design.md) is made.

| Runtime | Pinned version | Verified against | Date | Notes |
| --- | --- | --- | --- | --- |
| _tbd_ | | | | |

## CI: GitHub Actions

Actions used in workflows, pinned to a commit SHA (not a moving tag). Populated
when the CI skeleton lands.

| Action | Pinned SHA | Tag at pin | Verified against | Date |
| --- | --- | --- | --- | --- |
| `actions/checkout` | `df4cb1c069e1874edd31b4311f1884172cec0e10` | v6.0.3 | github.com/actions/checkout/releases/latest | 2026-06-17 |

The CI runner image is pinned too: workflows use `ubuntu-24.04`, never
`ubuntu-latest`.
