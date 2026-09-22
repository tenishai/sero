import { test } from "node:test";
import assert from "node:assert/strict";
import { isAdmin } from "./access.ts";
import type { Me } from "./api.ts";

function me(overrides: Partial<Me> = {}): Me {
  return {
    email: "op@example.com",
    namespaces: ["team-a"],
    is_admin: false,
    environment: "dev",
    ...overrides,
  };
}

test("isAdmin follows the backend's answer", () => {
  assert.equal(isAdmin(me({ is_admin: true })), true);
});

test("isAdmin is false for a member who is not an admin", () => {
  assert.equal(isAdmin(me()), false);
});

test("holding a namespace does not make you an admin", () => {
  assert.equal(isAdmin(me({ namespaces: ["platform-admin"] })), false);
});

test("isAdmin is false for an undefined user", () => {
  assert.equal(isAdmin(undefined), false);
});
