---
card: arch-card:sero-caller-identity
updated: 2026-09-28
anchor: src/sero/web/identity.py sha=e4a8dc093aea9a9e2cf3e65de9c188558762e7c8
anchor: src/sero/web/assertion.py sha=a69496d8e10509f35244ef67fd12f893e3957fdd
---
# caller-identity: who the human caller is (D15 in docs/specs/2026-09-22-platform-vault-design.md); no login form

WHAT / SEAM: `IdentityReader(header, dev_identity, assertion).email(headers)` (web/identity.py:78), used by web/deps.py:caller_email (→ `CallerEmail` dependency; 401 + audit row on failure). `AssertionVerifier` + `JwksCache` (web/assertion.py) verify a proxy-signed JWT; the machine lane reuses the same verifier class with its own settings. Authorization after identity: web/groups.py `GroupSource` (namespaces_for, is_admin), default `ConfigGroupSource` from config `access:`; MCP/web writes also check the datastore allow list (mcp/server.py:_require_allowed).
OPS / FLOW: if `identity.assertion` configured → read exactly one assertion header (default `x-goog-iap-jwt-assertion`), verify, take email claim; plain header ignored. Else → plain header (a claim, trusted only by network path) → else dev hatch → else 401. `normalize_email` strips `<issuer>:` prefix, lowercases.
INVARIANTS:
- Assertion configured ⇒ it is the ONLY source; header and dev hatch not read. WHY: proof beats claim; header is spoofable by anything reaching the port.
- Exactly one instance of the deciding header; two = refusal. WHY: duplicates mean something in the path is injecting headers.
- Algorithms pinned to ES256/RS256 (never from the token header); iss, aud, exp required; leeway 30s. WHY: blocks `none`/HS-with-public-key downgrade.
- JWKS: https only; TTL 900s; refetch floor 60s (anti-stampede); stale keys usable ≤ 3600s (machine lane: 600s) during key-endpoint outage.
- Dev hatch (SERO_DEV_IDENTITY) inert unless `identity.allow_dev_identity: true` AND no assertion; warns loudly.
- No assertion configured → startup warning; the backend must then be reachable only via the authenticating proxy (NetworkPolicy in k8s/base/networkpolicy.yaml, front door in deploy/sero/standalone/front-door/).
- Tokens never logged, including rejected ones.
CONSUMERS: web/deps.py (all /api routes except /service/*), web/service_lane.py (AssertionVerifier only), web/app.py wiring. Tests: tests/test_web_identity.py, tests/test_web_assertion.py, tests/fake_assertion.py.
