# Versioning

This document covers how Sero versions itself, how those versions relate to the
versions of the components it operates (OpenBao, the External Secrets Operator,
Helm charts, Terraform providers), and the pin-everything rule that keeps builds
reproducible.

## Sero's own version

Sero follows [Semantic Versioning 2.0.0](https://semver.org/): `MAJOR.MINOR.PATCH`.

- **MAJOR** changes when there is a breaking change to a public interface: the
  configuration schema a deployer provides, the CLI or API surface, the shape of
  rendered policies or ESO resources, or any default that changes behavior in a
  way a deployer must react to.
- **MINOR** changes for backward-compatible features.
- **PATCH** changes for backward-compatible fixes.

While Sero is pre-1.0 (`0.y.z`), the public surface is still settling. We treat
`0.MINOR` as the breaking-change axis and `0.y.PATCH` as additive or fixes, and
we call out breaks loudly in the changelog regardless.

A breaking change is signaled at commit time with Conventional Commits (`!` or a
`BREAKING CHANGE:` footer; see [CONTRIBUTING.md](../CONTRIBUTING.md)).

## Tags and releases

- Releases are cut from `main`. `main` is always releasable.
- Each release is an **annotated git tag** named `vMAJOR.MINOR.PATCH` (for
  example `v0.3.0`), so the tag carries a message and a date. Tags are not
  signed: provenance is the GitHub release and the DCO sign-off on every
  commit the tag reaches (decided 2026-10-18; `v0.1.0` was already unsigned).
- The tag points at the commit whose `CHANGELOG.md` has that version moved out of
  `Unreleased` into a dated section.

## CHANGELOG

We keep a human-curated changelog in [CHANGELOG.md](../CHANGELOG.md), following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Every user-facing
change adds an entry under `Unreleased` in the appropriate group (`Added`,
`Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`). Cutting a release
renames `Unreleased` to the version and date and starts a fresh `Unreleased`.

The changelog is for people. It is not a dump of commit subjects; it explains
what changed from a deployer's point of view.

## Component versions

Sero operates software it does not own: OpenBao, the External Secrets Operator,
their Helm charts, container images, Terraform providers, and the language
runtimes and GitHub Actions in the toolchain. These have their own version
numbers, independent of Sero's.

The relationship:

- **Sero's version describes Sero**, not the components. Upgrading the OpenBao
  chart Sero installs does not, by itself, dictate Sero's version bump; the
  bump is decided by how that change affects Sero's own public surface and
  defaults.
- **Every component version is pinned exactly** and recorded in
  [versions.md](versions.md). That file is the single source of truth for "what
  versions does this Sero operate against."
- A component bump is its own reviewed PR. It updates the pin in `versions.md`,
  adds a `CHANGELOG.md` entry, and bumps Sero's version per SemVer based on the
  observable effect (often MINOR or PATCH; MAJOR if a default or interface
  breaks).
- The compatibility a given Sero release was tested against is exactly the set
  of pins in `versions.md` at that tag.

## Pin everything

No floating `latest`, no unbounded version ranges, anywhere: OpenBao and its
chart, ESO and its chart, Terraform providers, container images (pin by digest
where practical, not just tag), language runtimes, and GitHub Actions (pin to a
commit SHA, not a moving tag).

When you introduce any new dependency, tool, chart, provider, action, or
runtime:

1. Web-verify its current latest stable from the authoritative source. Do not
   trust memory.
2. Pin to that exact version (or digest).
3. Record it in [versions.md](versions.md) with the date and the source you
   verified against.

Bumps are deliberate, reviewed PRs, never silent floats. This is what makes a
Sero tag reproducible months later.
