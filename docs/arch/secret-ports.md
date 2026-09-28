---
card: arch-card:ladon-secret-ports
updated: 2026-09-28
anchor: src/ladon/mcp/ports.py sha=93f2da16258ecfb6d5a952273f52c255e10fad05
anchor: src/ladon/mcp/openbao.py sha=97c3b5971e0fe1ef502035c3d21177703fdf1547
---
# secret-ports — the value-free boundary every surface reads and writes secrets through

WHAT / SEAM: mcp/ports.py Protocols (structural, unchecked at runtime):
- `SecretReader`: list_namespaces(email) · list_secrets(email, ns) → `SecretListing(secrets, truncated)` · get_secret(email, ns, path, env) → `SecretDetail` (masked_keys, version, updated_at, exists).
- `SecretWriter`: create_secret(email, ns, path, env, values) · update_secret(..., values, remove_keys) → `WriteResult` (key NAMES only).
- `SecretEraser`: delete_secret(email, ns, path) → None. Separate Protocol on purpose: write ≠ erase permission.
- `require_non_empty_merge(...)` — contract helper, not an impl detail. `SecretPathTaken(WriteRefused)` — create lost a race.
One impl: mcp/openbao.py `OpenBaoSecrets(client_for, namespaces_for, mount="kv")` — also carries two NON-port methods: `reveal_value` (the only plaintext exit, reveal route only) and `offboard_identity` (role+policy removal for the machine lane).
OPS / FLOW: surface (web/routes_secrets.py, web/service_lane.py, mcp/server.py tools) → mcp/server.py `*_impl` / `create_secret_write` / `update_secret_write` (allow-list, namespace gate, env check, placeholder + reserved-path refusal, audit) → port → hvac KV v2 in a per-namespace client (web/app.py:_live_secrets caches one client per namespace; token from env).
INVARIANTS:
- No plaintext crosses the read port: keys come back through hygiene.mask. WHY: every surface (UI, agents, machine lane) sits on this; one leak here leaks everywhere.
- create is conditional (`cas=0`), raises SecretPathTaken on conflict. WHY: check-then-write is two calls; without CAS two creates silently overwrite.
- update_secret must call require_non_empty_merge before writing. WHY: an emptied path still "exists" and reads as nothing; deleting is a separate explicit op (Eraser).
- `env` is a label, never a selector (openbao.py class doc). WHY: one Ladon = one OpenBao = one env; selecting on env would let two envs share paths.
- list_secrets walk is bounded (MAX_LIST_DEPTH=6, MAX_LISTED_SECRETS=500) and must report `truncated`. WHY: KV v2 lists one level at a time; unbounded walk = one namespace can pin a request.
- delete_secret is idempotent and must confirm absence after (raises SecretNotDeleted).
- web/app.py:REQUIRED_SECRET_METHODS (+ reveal_value when reveal enabled) is checked at startup: a wired impl missing a method refuses to boot rather than 500 on first call. Adding a port method → add it there.
CONSUMERS: web/routes_secrets.py (human lane), web/service_lane.py (machine lane), mcp/server.py (MCP tools, unserved), web/app.py (wiring). Tests: tests/fake_openbao.py, tests/fake_secrets.py implement the ports.
API: the three Protocols + dataclasses above; nothing outside this repo imports them.
