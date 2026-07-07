/**
 * Contract tests for the API client, run with the built-in node:test runner and
 * Node's native TypeScript type-stripping (no test-framework dependency).
 *
 * The transport is faked, so these assert the client maps the backend contract
 * (paths, response shapes, error handling) with no live backend.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { ApiClient, ApiError, type Transport } from "./api.ts";

/** A fake transport that records the requested path and returns a canned response. */
function fakeTransport(
  handler: (path: string) => { status?: number; body: unknown },
): { transport: Transport; calls: string[] } {
  const calls: string[] = [];
  const transport: Transport = async (path) => {
    calls.push(path);
    const { status = 200, body } = handler(path);
    return new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    });
  };
  return { transport, calls };
}

test("getMe returns the signed-in user from /api/me", async () => {
  const { transport, calls } = fakeTransport(() => ({
    body: { email: "op@example.com", namespaces: ["team-a"] },
  }));
  const client = new ApiClient(transport);
  const me = await client.getMe();
  assert.equal(me.email, "op@example.com");
  assert.deepEqual(calls, ["/api/me"]);
});

test("listNamespaces returns the namespaces array", async () => {
  const { transport, calls } = fakeTransport(() => ({
    body: { namespaces: ["team-a", "team-b"] },
  }));
  const client = new ApiClient(transport);
  assert.deepEqual(await client.listNamespaces(), ["team-a", "team-b"]);
  assert.deepEqual(calls, ["/api/namespaces"]);
});

test("listSecrets calls the namespaced path and returns secret paths", async () => {
  const { transport, calls } = fakeTransport(() => ({
    body: { namespace: "team-a", secrets: [{ path: "app", key_count: 2 }] },
  }));
  const client = new ApiClient(transport);
  const secrets = await client.listSecrets("team-a");
  assert.equal(secrets[0].path, "app");
  assert.equal(secrets[0].key_count, 2);
  assert.deepEqual(calls, ["/api/namespaces/team-a/secrets"]);
});

test("getSecret passes env and returns masked keys only", async () => {
  const { transport, calls } = fakeTransport(() => ({
    body: {
      namespace: "team-a",
      path: "app",
      env: "dev",
      exists: true,
      version: 3,
      key_count: 1,
      masked_keys: { API_KEY: "sup***" },
    },
  }));
  const client = new ApiClient(transport);
  const detail = await client.getSecret("team-a", "app", "dev");
  assert.equal(detail.exists, true);
  assert.deepEqual(detail.masked_keys, { API_KEY: "sup***" });
  assert.equal(calls[0], "/api/namespaces/team-a/secrets/app?env=dev");
});

test("path segments are URL-encoded", async () => {
  const { transport, calls } = fakeTransport(() => ({
    body: { namespace: "n", path: "a/b", env: "dev", exists: false, version: null,
            key_count: 0, masked_keys: {} },
  }));
  const client = new ApiClient(transport);
  await client.getSecret("team a", "a/b", "dev");
  assert.equal(calls[0], "/api/namespaces/team%20a/secrets/a%2Fb?env=dev");
});

test("requestNamespace posts the request and returns the pending result", async () => {
  const bodies: unknown[] = [];
  const transport: Transport = async (path, init) => {
    bodies.push(JSON.parse(String(init?.body)));
    assert.equal(init?.method, "POST");
    assert.equal(path, "/api/namespace-requests");
    return new Response(
      JSON.stringify({ request_id: 7, status: "pending", team_name: "team-x" }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  };
  const client = new ApiClient(transport);
  const result = await client.requestNamespace("team-x", ["dev"], ["a@example.com"], "why");
  assert.equal(result.status, "pending");
  assert.deepEqual(bodies[0], {
    team_name: "team-x", environments: ["dev"], members: ["a@example.com"], description: "why",
  });
});

test("listNamespaceRequests filters by status and unwraps the list", async () => {
  const { transport, calls } = fakeTransport(() => ({
    body: { requests: [{ id: 1, team_name: "t", status: "pending" }] },
  }));
  const client = new ApiClient(transport);
  const reqs = await client.listNamespaceRequests("pending");
  assert.equal(reqs[0].team_name, "t");
  assert.deepEqual(calls, ["/api/admin/namespace-requests?status=pending"]);
});

test("approve/reject post to the request action endpoints", async () => {
  const seen: string[] = [];
  const transport: Transport = async (path, init) => {
    seen.push(`${init?.method} ${path}`);
    return new Response(JSON.stringify({ id: 5, status: "submitted" }), {
      status: 200, headers: { "content-type": "application/json" },
    });
  };
  const client = new ApiClient(transport);
  await client.approveNamespaceRequest(5);
  await client.rejectNamespaceRequest(5, "nope");
  assert.deepEqual(seen, [
    "POST /api/admin/namespace-requests/5/approve",
    "POST /api/admin/namespace-requests/5/reject",
  ]);
});

test("allow-list add posts and remove deletes", async () => {
  const seen: string[] = [];
  const transport: Transport = async (path, init) => {
    seen.push(`${init?.method ?? "GET"} ${path}`);
    return new Response(JSON.stringify({ email: "x@example.com" }), {
      status: 200, headers: { "content-type": "application/json" },
    });
  };
  const client = new ApiClient(transport);
  await client.addToAllowList("x@example.com", "note");
  await client.removeFromAllowList("x@example.com");
  assert.deepEqual(seen, [
    "POST /api/admin/allow-list",
    "DELETE /api/admin/allow-list/x%40example.com",
  ]);
});

test("revealValue fetches one key's plaintext at the reveal endpoint", async () => {
  const { transport, calls } = fakeTransport(() => ({ body: { value: "s3cr3t-plain" } }));
  const client = new ApiClient(transport);
  const value = await client.revealValue("team-a", "app/gw", "dev", "API_KEY");
  assert.equal(value, "s3cr3t-plain");
  assert.equal(calls[0], "/api/namespaces/team-a/secrets/app%2Fgw/reveal?env=dev&key=API_KEY");
});

test("a non-ok response raises ApiError with the detail message", async () => {
  const { transport } = fakeTransport(() => ({ status: 403, body: { detail: "forbidden" } }));
  const client = new ApiClient(transport);
  await assert.rejects(
    () => client.listNamespaces(),
    (err: unknown) => err instanceof ApiError && err.status === 403 && err.message === "forbidden",
  );
});

test("createSecret posts to the namespace path and defaults to dry-run", async () => {
  const bodies: unknown[] = [];
  const transport: Transport = async (path, init) => {
    bodies.push(JSON.parse(String(init?.body)));
    assert.equal(init?.method, "POST");
    assert.equal(path, "/api/namespaces/team-a/secrets");
    return new Response(
      JSON.stringify({
        applied: false, action: "create", namespace: "team-a", path: "app", env: "dev",
        written_keys: ["API_KEY"], removed_keys: [],
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  };
  const client = new ApiClient(transport);
  const result = await client.createSecret("team-a", "app", "dev", { API_KEY: "v" });
  assert.equal(result.applied, false); // dry-run by default
  assert.deepEqual(bodies[0], {
    path: "app", env: "dev", values: { API_KEY: "v" }, apply: false,
  });
});

test("updateSecret PATCHes with merge and remove_keys and honors apply", async () => {
  const bodies: unknown[] = [];
  const transport: Transport = async (path, init) => {
    bodies.push(JSON.parse(String(init?.body)));
    assert.equal(init?.method, "PATCH");
    assert.equal(path, "/api/namespaces/team-a/secrets/app");
    return new Response(
      JSON.stringify({
        applied: true, action: "update", namespace: "team-a", path: "app", env: "dev",
        written_keys: ["NEW"], removed_keys: ["OLD"],
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  };
  const client = new ApiClient(transport);
  const result = await client.updateSecret("team-a", "app", "dev", { NEW: "v" }, ["OLD"], true);
  assert.equal(result.applied, true);
  assert.deepEqual(bodies[0], {
    env: "dev", values: { NEW: "v" }, remove_keys: ["OLD"], apply: true,
  });
});
