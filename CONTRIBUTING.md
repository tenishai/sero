# Contributing to Sero

Thanks for working on Sero. This guide covers how we develop: the branching
model, commit style, the review loop, and the bar a change must clear before it
merges. Read it once; it is short on purpose.

Sero is a secrets and vault management platform. Two rules sit above
everything else here and are repeated throughout:

- **Never commit secret values.** Not in code, config, tests, fixtures, commit
  messages, or PR text. See [Secrets hygiene](#secrets-hygiene).
- **Stay brand-neutral.** No company-specific names, domains, project or account
  IDs, cluster names, or internal identifiers. Sero is a product anyone can
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

Sign off every commit with `git commit -s`; see [Developer Certificate of Origin](#developer-certificate-of-origin).

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

Sero never stores secret values, and neither do contributions to it.

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

## Developer Certificate of Origin

Sero uses the [Developer Certificate of Origin](https://developercertificate.org/)
(DCO) instead of a contributor license agreement. Every commit in a pull request
must carry a `Signed-off-by:` line with your name and email, and they must match
the commit's author. `git commit -s` adds it from your `user.name` and
`user.email`:

```
Signed-off-by: Jane Doe <jane@example.com>
```

By adding that line you certify the following, quoted from
<https://developercertificate.org/>:

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

The `dco` CI check runs on every pull request to `main`. It checks each
non-merge commit the pull request adds, and fails naming each commit whose
sign-off is missing or doesn't match its author (name and email, compared
without regard to case). To fix it:

```bash
# the last commit only
git commit --amend --signoff --no-edit

# every commit on your branch (this signs every rebased commit as you, which
# satisfies the check only for commits whose author is you)
git rebase --signoff origin/main

# sign-off present but under a different name or email than the author:
# set user.name / user.email to the identity you want, then
git commit --amend --reset-author --signoff --no-edit

git push --force-with-lease
```

Commits already on `main` from before this rule are not re-checked. A pull request
opened by someone with write access to this repository (admin, maintain or
write; tenish's members all have it) skips the check, including one opened
from a fork; it is enforced on pull requests from everyone else. On a same-repo pull request, commits authored exactly as
Dependabot's noreply address are skipped too, because Dependabot cannot sign off
under that address.

## Using AI tools

AI-assisted contributions are welcome; we build this way ourselves. You are
responsible for every line you submit: understand it, test it, and be able to
explain it in review. Say in the pull request description which AI tools you
used. Crediting the agent with a `Co-Authored-By:` trailer is optional. Don't
submit code a tool reproduced from a source whose license isn't compatible with
AGPL-3.0.
