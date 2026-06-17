# Contributing to Ladon

Thanks for working on Ladon. This guide covers how we develop: the branching
model, commit style, the review loop, and the bar a change must clear before it
merges. Read it once; it is short on purpose.

Ladon is a secrets and vault management platform. Two rules sit above
everything else here and are repeated throughout:

- **Never commit secret values.** Not in code, config, tests, fixtures, commit
  messages, or PR text. See [Secrets hygiene](#secrets-hygiene).
- **Stay brand-neutral.** No company-specific names, domains, project or account
  IDs, cluster names, or internal identifiers. Ladon is a product anyone can
  deploy. See [Brand neutrality](#brand-neutrality).

## Development model

We use trunk-based development. `main` is always releasable. Work happens on
short-lived branches that merge back quickly.

1. Branch off `main`.
2. Make one small, single-purpose change.
3. Open a pull request against `main`.
4. Run `/code-review` on the diff and address what it finds.
5. Squash-merge once review passes and CI is green.
6. Delete the branch.

Keep branches short-lived: hours to a couple of days, not weeks. A long-running
branch is a sign the change is too big and should be split.

### Branch naming

Use a type prefix and a short kebab-case description:

| Prefix | For |
| --- | --- |
| `feat/` | A new capability |
| `fix/` | A bug fix |
| `docs/` | Documentation only |
| `chore/` | Tooling, scaffolding, dependencies, CI |
| `test/` | Tests only |

Examples: `feat/eso-secretstore-render`, `fix/policy-template-escaping`,
`docs/versioning`.

### Do not stack PRs deep

Prefer independent branches off `main`. If you must stack, merge the bottom of
the stack first and let the rest retarget before merging them. Verify each PR's
base is `main` immediately before merging.

## Commits

We follow [Conventional Commits](https://www.conventionalcommits.org/). The
subject is imperative and lowercase, with no trailing period:

```
<type>(<optional scope>): <imperative subject>

<body: what and why, wrapped at ~72 chars>
```

Types match the branch prefixes: `feat`, `fix`, `docs`, `chore`, `test`, plus
`refactor`, `build`, and `ci` where they fit. Breaking changes carry a `!` after
the type or scope (for example `feat!:`) and a `BREAKING CHANGE:` footer.

Commit and PR text must contain no em dashes (see below). The body should
explain why the change exists, not restate the diff.

## Hard conventions

These are enforced in CI by the conventions lint where a machine can check them,
and by reviewers otherwise.

### No em dashes

No em dashes in any prose: docs, README, UI copy, code comments meant as prose,
commit messages, or PR text. Use a spaced hyphen ( - ), a comma, or parentheses
instead. Em dashes inside code (string literals, identifiers) are fine.

### Brand neutrality

Nothing company-specific survives in the repo, commits, or PRs. No company or
product names, domains, GCP project IDs, AWS account IDs, cluster names, region
or environment names tied to a specific deployment, or internal identifiers.
Use generic, parameterized placeholders instead (for example
`<gcp-project-id>`, `example.com`, `cluster-a`). When in doubt, parameterize.

### Pin everything

Every dependency, tool, chart, provider, container image, language runtime, and
GitHub Action is pinned to an exact version. No floating `latest`, no unbounded
ranges. When you introduce something new, web-verify its current latest stable
from the authoritative source, pin to that exact version, and record it in
[docs/versions.md](docs/versions.md). Version bumps are their own reviewed PRs,
never silent floats. See [docs/versioning.md](docs/versioning.md).

### Secrets hygiene

Ladon never stores secret values, and neither do contributions to it.

- Never commit secret material. The `.gitignore` is a backstop, not permission
  to be careless; the CI check that no secret material is committed is the real
  gate.
- Never print or log secret values. If a tool must show that a key exists, mask
  it to the first few characters at most and treat the masked form as internal.
- Default to dry-run on any write. Show the namespace, path, and key names (not
  values) and the sync target before applying. Get explicit confirmation.
- Refuse placeholder values (`changeme`, `password`, `TODO`, empty). They are
  almost always a mistake.
- Least privilege everywhere: request the narrowest policy, role, and scope that
  does the job.

## Definition of done

A pull request is ready to merge when all of the following hold:

- [ ] The change is single-purpose and the branch is named with the right prefix.
- [ ] Commits follow Conventional Commits and contain no em dashes.
- [ ] `/code-review` has been run on the diff and its findings are resolved.
- [ ] CI is green (static checks at minimum: lint, conventions, no committed
      secret material, no floating pins).
- [ ] No secret values appear anywhere in the diff, history, or PR text.
- [ ] No company-specific identifiers appear anywhere in the diff or PR text.
- [ ] New dependencies are pinned and recorded in
      [docs/versions.md](docs/versions.md).
- [ ] Docs are updated when behavior or interfaces change.
- [ ] The PR description explains what changed and why.

## Review loop

Every diff goes through build, then review, then revise:

1. **Build** the smallest change that accomplishes the goal.
2. **Review** by running `/code-review` and reading its findings honestly.
3. **Revise** until the findings are resolved or consciously accepted with a
   reason recorded in the PR.

Reviews look for correctness first, then simplicity, reuse, and efficiency.
Coverage theater and speculative generality are not improvements.
