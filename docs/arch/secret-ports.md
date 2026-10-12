---
card: arch-card:sero-secret-ports
updated: 2026-09-28
anchor: src/sero/mcp/ports.py sha=d110b5b4457fe3137498e5e272a11af11519e84e
anchor: src/sero/mcp/openbao.py sha=a704ff506239c512e1b09841ca64a7a4501d4867
---
# secret-ports: the boundary every surface reads and writes secrets through (reads are masked, not value-free)

WHAT / SEAM: mcp/ports.py Protocols (structural, unchecked at runtime):
- `SecretReader`: list_namespaces(email) · list_secrets(email, ns) → `SecretListing(secrets, truncated)` · get_secret(email, ns, path, env) → `SecretDetail` (masked_keys, version, updated_at, exists).
- `SecretWriter`: create_secret(email, ns, path, env, values) · update_secret(..., values, remove_keys) → `WriteResult` (key NAMES only).
- `SecretEraser`: delete_secret(email, ns, path) → None. Separate Protocol on purpose: write ≠ erase permission.
- `require_non_empty_merge(...)`: contract helper, not an impl detail. `SecretPathTaken(WriteRefused)`: create lost a race.
One impl: mcp/openbao.py `OpenBaoSecrets(client_for, namespaces_for, mount="kv")`, which also carries two NON-port methods: `reveal_value` (the only plaintext exit, reveal route only) and `offboard_identity` (role+policy removal for the machine lane).
OPS / FLOW: surface (web/routes_secrets.py, web/service_lane.py, mcp/server.py tools) → mcp/server.py. The `*_impl` wrappers apply the allow list (`_require_allowed`) and the namespace gate (`_require_namespace`), then call `create_secret_write` / `update_secret_write`. The `*_write` functions do NOT apply the allow list or the namespace gate: they are for a caller already authorized, and do only the env check, placeholder + reserved-path refusal and audit. The human lane (web/routes_secrets.py) calls `*_impl`; the machine lane imports only the `*_write` forms and gates on its caller's pinned namespaces instead (web/service_lane.py `_require_pinned`), so the machine lane is NOT allow-listed → port → hvac KV v2 in a per-namespace client (web/app.py:_live_secrets caches one client per namespace; token from env).
INVARIANTS:
- The read port returns masked values, not nothing: `mask` (src/sero/hygiene.py) returns the first three characters of a value in clear, then stars to its full length (a value of three characters or fewer is all stars). A read therefore exposes a prefix and the length. WHY: every surface (UI, agents, machine lane) sits on this; one leak here leaks everywhere.
- create is conditional (`cas=0`), raises SecretPathTaken on conflict. WHY: check-then-write is two calls; without CAS two creates silently overwrite.
- update_secret must call require_non_empty_merge before writing. WHY: an emptied path still "exists" and reads as nothing; deleting is a separate explicit op (Eraser).
- `env` is a label, never a selector (openbao.py class doc). WHY: one Sero = one OpenBao = one env; selecting on env would let two envs share paths.
- list_secrets walk is bounded (MAX_LIST_DEPTH=6, MAX_LISTED_SECRETS=500) and must report `truncated`. WHY: KV v2 lists one level at a time; unbounded walk = one namespace can pin a request.
- delete_secret is idempotent and must confirm absence after (raises SecretNotDeleted).
- web/app.py:REQUIRED_SECRET_METHODS (+ reveal_value when reveal enabled) is checked at startup: a wired impl missing a method refuses to boot rather than 500 on first call. Adding a port method → add it there.
CONSUMERS: web/routes_secrets.py (human lane), web/service_lane.py (machine lane), mcp/server.py (MCP tools, unserved), web/app.py (wiring). Tests: tests/fake_openbao.py, tests/fake_secrets.py implement the ports.
API: the three Protocols + dataclasses above; nothing outside this repo imports them.
