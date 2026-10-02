# Security policy

## Reporting a vulnerability

Please don't open a public issue for a security problem.

Report it privately to **security@tenish.ai**.

If this repository's **Security** tab shows a **Report a vulnerability**
button, you can use that instead. GitHub offers it only on public
repositories where it has been turned on.

Include what you can of:

- what the problem is and what an attacker could do with it;
- the version, commit, or tag you tested;
- steps to reproduce, or a proof of concept;
- any configuration it depends on.

You don't need a complete write-up to report. A clear description of the
problem is enough to start.

## What happens next

- We acknowledge your report within **14 days**.
- We tell you whether we've confirmed the problem as soon as we know.
- We fix confirmed problems in the next release, or sooner for serious ones,
  and tell you before the fix is public.
- We credit you in the release notes unless you'd rather we didn't.

We ask that you give us a reasonable chance to fix a problem before disclosing
it publicly, and that you don't access or change data that isn't yours while
testing.

## Supported versions

ladon is pre-1.0. Security fixes go into the **latest release** only; older
releases are not patched. If you're running an older version, upgrade to get
fixes.

## Scope

ladon is the secrets lane: it carries secrets from the vault to the clusters
and apps that need them, by namespace. In scope:

- confidentiality: a secret value leaving the lane in the clear, in a log, an
  error, a response, or an audit record;
- isolation: a namespace, app or caller reading or writing secrets it was not
  granted, including through sync, offboarding, or an implicit onboarding;
- the lane itself: a sync that falls back to a direct write when the lane
  refuses, or a refusal that is reported as success;
- identity: a token or service identity outliving its intended life, or being
  minted for the wrong principal.

**Out of scope:** vulnerabilities in third-party dependencies (please report
those upstream, and tell us if we ship an affected version), and problems that
require an attacker to already control the cluster, the operator account, or
the identity the service runs under.
