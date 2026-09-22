import { test } from "node:test";
import assert from "node:assert/strict";
import { defaultEnvironment, environmentsFor } from "./environments.ts";
import { branding } from "../branding.ts";
import type { Me } from "./api.ts";

function me(environment: string | null): Me {
  return {
    email: "op@example.com",
    namespaces: ["team-a"],
    is_admin: false,
    environment,
  };
}

test("a deployment that declares an environment offers only that one", () => {
  assert.deepEqual(environmentsFor(me("prod")), ["prod"]);
  assert.equal(defaultEnvironment(me("prod")), "prod");
});

test("an undeclared environment falls back to the branding list", () => {
  assert.deepEqual(environmentsFor(me(null)), branding.environments);
  assert.equal(defaultEnvironment(me(null)), branding.environments[0]);
});

test("before /api/me answers, the branding list stands in", () => {
  assert.deepEqual(environmentsFor(undefined), branding.environments);
  assert.equal(defaultEnvironment(undefined), branding.environments[0]);
});

test("the served environment need not be one the branding list knows", () => {
  // A deployment is free to call its environment anything; the switcher must
  // follow the backend rather than the other way round.
  assert.deepEqual(environmentsFor(me("staging-eu")), ["staging-eu"]);
});
