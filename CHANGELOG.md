# Changelog

All notable changes to Ladon are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

This changelog is for people. Entries explain what changed from a deployer's
point of view, not a list of commit subjects.

## [Unreleased]

### Added

- Repository foundations: Apache-2.0 license, NOTICE, README, secrets-hygiene
  `.gitignore`, `.gitattributes`, and `.editorconfig`.
- Project-local Vercel `web-design-guidelines` skill and `docs/skills.md`.
- `CONTRIBUTING.md` documenting the trunk-based development model, Conventional
  Commits, hard conventions, and the per-PR definition of done.
- Versioning policy (`docs/versioning.md`), this changelog, and the pinned
  versions reference (`docs/versions.md`).
- Conventions lint (`scripts/lint-conventions.sh`) enforcing no em dashes, no
  company-specific identifiers, no floating version pins, and no committed
  secret material.
- CI skeleton (`.github/workflows/ci.yml`) running the static layer on every
  pull request and push to `main`, with actions pinned by commit SHA.

[Unreleased]: https://github.com/kmgreiner3/ladon/commits/main
