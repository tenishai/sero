import { test } from "node:test";
import assert from "node:assert/strict";
import { isAdmin } from "./access.ts";
import { branding } from "../branding.ts";

const ADMIN = branding.adminNamespace;

test("isAdmin is true when the admin namespace is present", () => {
  assert.equal(isAdmin({ email: "op@example.com", namespaces: ["team-a", ADMIN] }), true);
});

test("isAdmin is false without the admin namespace", () => {
  assert.equal(isAdmin({ email: "op@example.com", namespaces: ["team-a"] }), false);
});

test("isAdmin is false for an undefined user", () => {
  assert.equal(isAdmin(undefined), false);
});
