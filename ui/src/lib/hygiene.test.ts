import { test } from "node:test";
import assert from "node:assert/strict";
import { isPlaceholder, placeholderKeys } from "./hygiene.ts";

test("isPlaceholder flags common fillers, case- and space-insensitively", () => {
  for (const v of ["changeme", "CHANGEME", "  TODO ", "password", "", "replace_me"]) {
    assert.equal(isPlaceholder(v), true, `${JSON.stringify(v)} should be a placeholder`);
  }
});

test("isPlaceholder accepts real-looking values", () => {
  for (const v of ["s3cr3t-value-1", "an-actual-api-key", "hunter2!"]) {
    assert.equal(isPlaceholder(v), false, `${JSON.stringify(v)} should be allowed`);
  }
});

test("placeholderKeys returns only the offending keys", () => {
  const keys = placeholderKeys({ REAL: "actual-value-99", BAD: "changeme", EMPTY: "" });
  assert.deepEqual(keys.sort(), ["BAD", "EMPTY"]);
});

test("placeholderKeys is empty when all values are real", () => {
  assert.deepEqual(placeholderKeys({ A: "real-1234", B: "real-5678" }), []);
});
