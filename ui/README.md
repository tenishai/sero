# Sero UI (D7)

A lean, unbranded operator interface for the OpenBao secrets platform, built to
drop into any deployment and rebrand in two files.

## Stack

Svelte 5 (runes) + Vite + TypeScript + Tailwind CSS v4 + TanStack Query. One
runtime dependency (`@tanstack/svelte-query`); everything else is dev tooling.
Versions are pinned exactly (see `package.json` and `docs/versions.md`).

## Rebranding: two files, no component edits

Sero ships unbranded. To make the UI a client's own:

1. **`src/app.css`** - the palette, fonts, and radii, as CSS variables in the
   `@theme` block (and the `html.theme-light` overrides). Components reference
   these tokens and never hardcode a color, so changing the look is changing this
   file. Also update the favicon at `public/favicon.svg`.
2. **`src/branding.ts`** - product name, tagline, the admin-namespace concept,
   the environment list, doc links, and the storage-key prefix. Components and
   views import from here; they never hardcode identity.

`index.html` carries a static title and a pre-paint theme script for the initial
render; keep its title and the `sero:theme` storage key in sync with
`branding.ts` when rebranding.

Nothing in the rest of the tree hardcodes a company, cloud, product name, or
color. That separation is the "drop it in, rebrand, onboard" contract.

## Scope

The baseline UI is a generic secrets-admin surface: namespaces, secrets (names
and masked previews only, never values), create/update (dry-run first), the
namespace-request flow, admin, and login. Cloud-specific surfaces (per-provider
sync, cluster actions) are out of the baseline and reintroduced later as optional
modules, so the core stays lean and cloud-neutral.

Secrets hygiene holds in the UI as in the backend: a secret value is never shown
by default; reveal (when supported) is an explicit, on-demand, ephemeral action
and plaintext is never persisted or cached.

## Develop

```
npm install
npm run dev       # dev server, proxies /api to the backend
npm run check     # svelte-check (types)
npm run build     # type-check + production build
```
