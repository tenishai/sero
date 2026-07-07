import { test } from "node:test";
import assert from "node:assert/strict";
import { AlwaysAllowGate } from "./revealGate.ts";

test("AlwaysAllowGate always reports an active, non-expiring grant", async () => {
  const gate = new AlwaysAllowGate();
  assert.deepEqual(await gate.status(), { active: true, expiresAt: null });
  assert.deepEqual(await gate.ensureActive(), { active: true, expiresAt: null });
});

test("AlwaysAllowGate revoke is a no-op that resolves", async () => {
  const gate = new AlwaysAllowGate();
  await gate.revoke(); // must not throw
  // Still active after "revoke": this gate always permits reveal.
  assert.equal((await gate.status()).active, true);
});
