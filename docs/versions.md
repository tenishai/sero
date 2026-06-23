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
| OpenBao | 2.5.5 | github.com/openbao/openbao-helm/releases (chart 0.28.4 bundles 2.5.5) | 2026-06-17 | Server and CLI. Image tag pinned in deploy/openbao/values/values-base.yaml. |
| OpenBao Helm chart | 0.28.4 | github.com/openbao/openbao-helm/releases | 2026-06-17 | `openbao/openbao` chart. Apply with `--version 0.28.4`; never a floating chart. |
| External Secrets Operator | _tbd_ | | | |
| ESO Helm chart | _tbd_ | | | |
| cert-manager | _tbd_ | | | If adopted |

## Infrastructure providers

Terraform and its providers. Populated as the substrate layer (D10) lands.

| Provider | Pinned version | Verified against | Date | Notes |
| --- | --- | --- | --- | --- |
| Terraform | 1.15.6 | developer.hashicorp.com/terraform/install | 2026-06-23 | `required_version >= 1.15.6` in the substrate modules. |
| hashicorp/google | 7.37.0 | registry.terraform.io/providers/hashicorp/google | 2026-06-23 | Substrate (D10). Pinned exactly in module `versions.tf`. |

## Terraform modules

Official cloud-provider registry modules, preferred over hand-rolled resource
blocks. Pin each exactly; module bumps are their own reviewed PRs.

| Module | Pinned version | Verified against | Date | Notes |
| --- | --- | --- | --- | --- |
| terraform-google-modules/kms/google | 4.1.2 | registry.terraform.io/modules/terraform-google-modules/kms/google | 2026-06-23 | OpenBao auto-unseal key (D2a), `terraform/modules/unseal-key/gcp`. Keyring, key, and encrypter/decrypter IAM. |

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
| Python | 3.14.6 | python.org/downloads | 2026-06-17 | `requires-python >= 3.14`; `.python-version` pins 3.14. |
| uv | 0.11.21 | github.com/astral-sh/uv/releases/latest | 2026-06-17 | Package and environment manager. |
| ruff | 0.15.17 | pypi.org/project/ruff | 2026-06-17 | Lint and format. Pinned in pyproject dev group and uv.lock. |
| pytest | 9.1.0 | pypi.org/project/pytest | 2026-06-17 | Test runner. Pinned in pyproject dev group and uv.lock. |
| PyYAML | 6.0.3 | pypi.org/project/PyYAML | 2026-06-17 | Runtime dependency: parse deployment config. Pinned in pyproject and uv.lock. |
| hvac | 2.4.0 | pypi.org/project/hvac | 2026-06-22 | Runtime dependency: OpenBao API client for the SDK apply layer (D3b). The pure plan layer landed first; the client wrapper follows. Pinned in pyproject and uv.lock. |

## Tooling and CLIs

Command-line tools the apply layers invoke. Pin the exact version in tooling and
container images; do not rely on whatever is on the path.

| Tool | Pinned version | Verified against | Date | Notes |
| --- | --- | --- | --- | --- |
| Helm | 4.2.1 | github.com/helm/helm/releases/latest | 2026-06-17 | Used for the OpenBao install layer (D3a). |

## CI: GitHub Actions

Actions used in workflows, pinned to a commit SHA (not a moving tag). Populated
when the CI skeleton lands.

| Action | Pinned SHA | Tag at pin | Verified against | Date |
| --- | --- | --- | --- | --- |
| `actions/checkout` | `df4cb1c069e1874edd31b4311f1884172cec0e10` | v6.0.3 | github.com/actions/checkout/releases/latest | 2026-06-17 |
| `Azure/setup-helm` | `dda3372f752e03dde6b3237bc9431cdc2f7a02a2` | v5.0.0 | github.com/Azure/setup-helm/releases/latest | 2026-06-17 |
| `astral-sh/setup-uv` | `fac544c07dec837d0ccb6301d7b5580bf5edae39` | v8.2.0 | github.com/astral-sh/setup-uv/releases/latest | 2026-06-17 |
| `hashicorp/setup-terraform` | `dfe3c3f87815947d99a8997f908cb6525fc44e9e` | v4.0.1 | github.com/hashicorp/setup-terraform/releases/latest | 2026-06-23 |

The CI runner image is pinned too: workflows use `ubuntu-24.04`, never
`ubuntu-latest`.
