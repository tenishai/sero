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
| External Secrets Operator | 2.6.0 | github.com/external-secrets/external-secrets/releases (chart 2.6.0 bundles app v2.6.0) | 2026-06-23 | Image `ghcr.io/external-secrets/external-secrets:v2.6.0`. Installed via the chart (D4a). |
| ESO Helm chart | 2.6.0 | github.com/external-secrets/external-secrets/releases | 2026-06-23 | `external-secrets/external-secrets` chart from `https://charts.external-secrets.io`. Apply with `--version 2.6.0`; never a floating chart. Pinned in src/ladon/install/eso.py. |
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
| SQLAlchemy | 2.0.51 | pypi.org/project/SQLAlchemy | 2026-07-06 | Runtime dependency: ORM/schema for the backend datastore (D11). Pinned in pyproject and uv.lock. |
| alembic | 1.18.5 | pypi.org/project/alembic | 2026-07-06 | Runtime dependency: datastore migrations (D11). Pinned in pyproject and uv.lock. |
| mcp | 1.28.1 | pypi.org/project/mcp | 2026-07-06 | Runtime dependency: MCP server (FastMCP) for the agent surface (D12). v1.x is the recommended production line (2.0 is prerelease). Pinned in pyproject and uv.lock. |
| PyJWT | 2.13.0 | pypi.org/project/PyJWT | 2026-07-06 | Runtime dependency: verify MCP OAuth 2.1 bearer access tokens (D12). Also a transitive dep of mcp; pinned directly since Ladon imports it. Pinned in pyproject and uv.lock. |

## Frontend (UI)

The Svelte UI (D7) under `ui/`. Pinned exactly in `ui/package.json` and
`ui/package-lock.json`; installed with `npm ci`. One runtime dependency; the rest
is dev tooling.

| Package | Pinned version | Verified against | Date | Notes |
| --- | --- | --- | --- | --- |
| svelte | 5.56.4 | npmjs.com/package/svelte | 2026-07-06 | Svelte 5 (runes). |
| vite | 8.1.3 | npmjs.com/package/vite | 2026-07-06 | Build tool and dev server. |
| typescript | 6.0.3 | npmjs.com/package/typescript | 2026-07-06 | |
| tailwindcss | 4.3.2 | npmjs.com/package/tailwindcss | 2026-07-06 | v4, configured in CSS (`ui/src/app.css`); no config file. |
| @tailwindcss/vite | 4.3.2 | npmjs.com/package/@tailwindcss/vite | 2026-07-06 | Tailwind Vite plugin. |
| @sveltejs/vite-plugin-svelte | 7.1.4 | npmjs.com/package/@sveltejs/vite-plugin-svelte | 2026-07-06 | |
| svelte-check | 4.7.1 | npmjs.com/package/svelte-check | 2026-07-06 | Type-check in `npm run build`. |
| @tsconfig/svelte | 5.0.8 | npmjs.com/package/@tsconfig/svelte | 2026-07-06 | Base tsconfig. |
| @types/node | 26.1.0 | npmjs.com/package/@types/node | 2026-07-07 | Types for the node:test API-client tests (run via Node type-stripping, no test framework). |
| @playwright/test | 1.61.1 | npmjs.com/package/@playwright/test | 2026-07-07 | Render smoke test (e2e/): loads the app in a headless browser to catch render-time failures CI's build/type-check miss. |
| @tanstack/svelte-query | 6.1.36 | npmjs.com/package/@tanstack/svelte-query | 2026-07-06 | The one runtime dependency: server-state/query layer. |

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
| `actions/setup-node` | `48b55a011bda9f5d6aeb4c2d9c7362e8dae4041e` | v6.4.0 | github.com/actions/setup-node/releases/latest | 2026-07-06 |

The CI runner image is pinned too: workflows use `ubuntu-24.04`, never
`ubuntu-latest`.
