/**
 * A dev-only fake transport for the API client.
 *
 * With no backend running, this returns canned, obviously-fake data so the whole
 * UI renders fully populated for local preview and design work. It is wired in
 * ONLY in dev mode (see main.ts, guarded by import.meta.env.DEV), so it is never
 * part of a production build and never reaches a real deployment.
 *
 * Nothing here is a real secret: "values" are already-masked previews, exactly
 * as the backend would return them. Writes are echoed back as value-free
 * summaries, so the dry-run/apply flow is exercisable without persisting
 * anything.
 */

import type { Transport } from "./api.ts";

interface MockSecret {
  keys: Record<string, string>; // key -> masked preview
  version: number;
}

// A small, clearly-fake dataset. Namespaces include the admin namespace so the
// "admin" badge shows in the preview.
const NAMESPACES = ["platform-admin", "team-payments", "team-web"];

const MOCK_REQUESTS = [
  {
    id: 42, requester_email: "newlead@example.com", team_name: "team-mobile",
    environments: ["dev", "qa", "prod"], members: ["newlead@example.com", "dev2@example.com"],
    description: "Secrets for the new mobile backend.", status: "pending", created_at: "2026-07-07",
  },
  {
    id: 43, requester_email: "data@example.com", team_name: "team-analytics",
    environments: ["dev"], members: ["data@example.com"], description: null,
    status: "pending", created_at: "2026-07-07",
  },
];

const MOCK_ALLOW = [
  { email: "you@example.com", added_by: "bootstrap", added_at: "2026-06-01", notes: "platform admin" },
  { email: "agent-bot@example.com", added_by: "you@example.com", added_at: "2026-07-01", notes: "CI agent" },
];

const SECRETS: Record<string, Record<string, MockSecret>> = {
  "team-payments": {
    "app/gateway": {
      version: 4,
      keys: { STRIPE_KEY: "sk_***", WEBHOOK_SECRET: "whs***", TIMEOUT_MS: "300" },
    },
    "app/database": { version: 2, keys: { DB_URL: "pos***", DB_PASSWORD: "***" } },
  },
  "team-web": {
    "app/frontend": { version: 1, keys: { API_BASE: "htt***", SENTRY_DSN: "htt***" } },
  },
  "platform-admin": {},
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** A `fetch`-compatible transport that answers the API contract from canned data. */
export const devMockTransport: Transport = async (path, init) => {
  const url = new URL(path, "http://mock");
  const p = url.pathname.replace(/^\/api/, "");
  const method = (init?.method ?? "GET").toUpperCase();

  if (p === "/me") {
    return json({ email: "you@example.com", namespaces: NAMESPACES });
  }
  if (p === "/namespaces") {
    return json({ namespaces: NAMESPACES });
  }

  if (p === "/namespace-requests" && method === "POST") {
    const body = JSON.parse(String(init?.body ?? "{}"));
    return json({ request_id: 101, status: "pending", team_name: body.team_name ?? "" });
  }

  // Admin routes.
  if (p === "/admin/namespace-requests") {
    return json({ requests: MOCK_REQUESTS });
  }
  const reqAction = p.match(/^\/admin\/namespace-requests\/(\d+)\/(approve|reject)$/);
  if (reqAction && method === "POST") {
    return json({ ...MOCK_REQUESTS[0], id: Number(reqAction[1]), status: reqAction[2] });
  }
  if (p === "/admin/allow-list") {
    if (method === "POST") {
      const body = JSON.parse(String(init?.body ?? "{}"));
      return json({ email: body.email, added_by: "you@example.com", added_at: "now", notes: body.notes ?? null });
    }
    return json({ entries: MOCK_ALLOW });
  }
  if (p.startsWith("/admin/allow-list/") && method === "DELETE") {
    return new Response(null, { status: 204 });
  }

  const listMatch = p.match(/^\/namespaces\/([^/]+)\/secrets$/);
  if (listMatch) {
    const ns = decodeURIComponent(listMatch[1]);
    if (method === "POST") return writeSummary(url, init, "create");
    const secrets = Object.entries(SECRETS[ns] ?? {}).map(([path, s]) => ({
      path,
      key_count: Object.keys(s.keys).length,
    }));
    return json({ namespace: ns, secrets });
  }

  const revealMatch = p.match(/^\/namespaces\/([^/]+)\/secrets\/(.+)\/reveal$/);
  if (revealMatch) {
    const key = url.searchParams.get("key") ?? "";
    // An obviously-fake plaintext for preview; not a real secret.
    return json({ value: `mock-value-for-${key}` });
  }

  const detailMatch = p.match(/^\/namespaces\/([^/]+)\/secrets\/(.+)$/);
  if (detailMatch) {
    const ns = decodeURIComponent(detailMatch[1]);
    const secretPath = decodeURIComponent(detailMatch[2]);
    if (method === "PATCH") return writeSummary(url, init, "update");
    const env = url.searchParams.get("env") ?? "dev";
    const secret = SECRETS[ns]?.[secretPath];
    if (!secret) {
      return json({
        namespace: ns, path: secretPath, env, exists: false, version: null,
        key_count: 0, masked_keys: {},
      });
    }
    return json({
      namespace: ns, path: secretPath, env, exists: true, version: secret.version,
      key_count: Object.keys(secret.keys).length, masked_keys: secret.keys,
    });
  }

  return json({ detail: `mock: no route for ${method} ${p}` }, 404);
};

function writeSummary(url: URL, init: RequestInit | undefined, action: "create" | "update"): Response {
  const body = JSON.parse(String(init?.body ?? "{}"));
  return json({
    applied: body.apply === true,
    action,
    namespace: url.pathname.split("/")[3] ?? "",
    path: body.path ?? url.pathname.split("/").slice(5).join("/"),
    env: body.env ?? "dev",
    written_keys: Object.keys(body.values ?? {}),
    removed_keys: body.remove_keys ?? [],
  });
}
