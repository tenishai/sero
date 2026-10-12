/**
 * Typed API client for the Sero backend.
 *
 * A thin wrapper over `fetch` with typed responses, mapping to the backend's
 * read/write contract (the same operations the MCP tools expose). The transport
 * is injectable: production uses `fetch` against a same-origin relative `/api`
 * base; tests and previews inject a fake so the client, query layer, and views
 * run with no live backend.
 *
 * Secrets hygiene holds here: a secret detail carries masked key names only
 * (`masked_keys`), never a plaintext value. Revealing a value is a separate,
 * explicit action (a later slice), never part of listing or detail.
 */

/** The transport a client uses. `fetch`-compatible, so production passes `fetch`. */
export type Transport = (path: string, init?: RequestInit) => Promise<Response>;

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/**
 * The signed-in user, as surfaced by the backend from the identity the proxy
 * established (see the auth model: no login form, the proxy owns login).
 * `namespaces` are those the user may access. `is_admin` is the backend's
 * answer, not a guess from namespace membership: admins come from the
 * deployment config and hold no namespace by virtue of being admins.
 * `environment` is the single environment this deployment serves; a request
 * naming another is refused.
 */
export interface Me {
  email: string;
  namespaces: string[];
  is_admin: boolean;
  environment: string | null;
}

export interface NamespacesResponse {
  namespaces: string[];
}

export interface SecretPath {
  path: string;
  key_count: number;
}

export interface SecretsResponse {
  namespace: string;
  secrets: SecretPath[];
  // False when the listing is the whole namespace. True when the backend's
  // walk stopped at one of its bounds, so there are secrets it did not report.
  truncated?: boolean;
}

export interface SecretsListing {
  secrets: SecretPath[];
  truncated: boolean;
}

export interface SecretDetail {
  namespace: string;
  path: string;
  env: string;
  exists: boolean;
  version: number | null;
  key_count: number;
  /** Key name to masked preview. Never a plaintext value. */
  masked_keys: Record<string, string>;
}

/**
 * The value-free result of a write (create or update), matching the backend
 * write tools. `applied` is false for a dry-run (a preview of what would change)
 * and true once committed. Only key names appear, never a value.
 */
export interface WriteSummary {
  applied: boolean;
  action: "create" | "update";
  namespace: string;
  path: string;
  env: string;
  written_keys: string[];
  removed_keys: string[];
}

/** The result of requesting a namespace: a pending request an admin will action. */
export interface NamespaceRequestResult {
  request_id: number;
  status: string;
  team_name: string;
}

/** A namespace request row, as an admin sees it in the review queue. */
export interface NamespaceRequestRow {
  id: number;
  requester_email: string;
  team_name: string;
  environments: string[];
  members: string[];
  description: string | null;
  status: string;
  created_at: string;
}

/** An MCP allow-list entry: an identity permitted to use the agent surface. */
export interface AllowListEntry {
  email: string;
  added_by: string;
  added_at: string;
  notes: string | null;
}

const DEFAULT_BASE = "/api";

export class ApiClient {
  private readonly transport: Transport;
  private readonly base: string;

  constructor(transport: Transport = fetch.bind(globalThis), base: string = DEFAULT_BASE) {
    this.transport = transport;
    this.base = base;
  }

  /** The signed-in user, from the proxy-supplied identity the backend exposes. */
  getMe(): Promise<Me> {
    return this.get<Me>("/me");
  }

  async listNamespaces(): Promise<string[]> {
    const body = await this.get<NamespacesResponse>("/namespaces");
    return body.namespaces;
  }

  async listSecrets(namespace: string): Promise<SecretsListing> {
    const body = await this.get<SecretsResponse>(
      `/namespaces/${encodeURIComponent(namespace)}/secrets`,
    );
    // Truncation travels with the list rather than being dropped here: a view
    // that renders a short list without saying it is short is the bug.
    return { secrets: body.secrets, truncated: body.truncated === true };
  }

  async getSecret(namespace: string, path: string, env: string): Promise<SecretDetail> {
    return this.get<SecretDetail>(
      `/namespaces/${encodeURIComponent(namespace)}/secrets/${encodeURIComponent(path)}` +
        `?env=${encodeURIComponent(env)}`,
    );
  }

  /**
   * Create a secret path. `apply=false` (the default) is a dry-run: the backend
   * returns a value-free summary of what would change and writes nothing.
   */
  createSecret(
    namespace: string,
    path: string,
    env: string,
    values: Record<string, string>,
    apply = false,
  ): Promise<WriteSummary> {
    return this.send<WriteSummary>(
      "POST",
      `/namespaces/${encodeURIComponent(namespace)}/secrets`,
      { path, env, values, apply },
    );
  }

  /**
   * Update a secret path. Merges: keys in `values` are written, keys in
   * `remove_keys` deleted, others left untouched. `apply=false` is a dry-run.
   */
  updateSecret(
    namespace: string,
    path: string,
    env: string,
    values: Record<string, string>,
    removeKeys: string[],
    apply = false,
  ): Promise<WriteSummary> {
    return this.send<WriteSummary>(
      "PATCH",
      `/namespaces/${encodeURIComponent(namespace)}/secrets/${encodeURIComponent(path)}`,
      { env, values, remove_keys: removeKeys, apply },
    );
  }

  /** Request a new team namespace. Records a pending request for an admin. */
  requestNamespace(
    teamName: string,
    environments: string[],
    members: string[],
    description?: string,
  ): Promise<NamespaceRequestResult> {
    return this.send<NamespaceRequestResult>("POST", "/namespace-requests", {
      team_name: teamName,
      environments,
      members,
      description,
    });
  }

  // ── Admin ──────────────────────────────────────────────────────────────

  /** List namespace requests, optionally filtered by status (e.g. "pending"). */
  listNamespaceRequests(status?: string): Promise<NamespaceRequestRow[]> {
    const q = status ? `?status=${encodeURIComponent(status)}` : "";
    return this.get<{ requests: NamespaceRequestRow[] }>(`/admin/namespace-requests${q}`).then(
      (r) => r.requests,
    );
  }

  /** Approve a request (mark it submitted for provisioning). */
  approveNamespaceRequest(id: number): Promise<NamespaceRequestRow> {
    return this.send<NamespaceRequestRow>(
      "POST",
      `/admin/namespace-requests/${id}/approve`,
      {},
    );
  }

  /** Reject a request with a reason. */
  rejectNamespaceRequest(id: number, reason: string): Promise<NamespaceRequestRow> {
    return this.send<NamespaceRequestRow>("POST", `/admin/namespace-requests/${id}/reject`, {
      reason,
    });
  }

  listAllowList(): Promise<AllowListEntry[]> {
    return this.get<{ entries: AllowListEntry[] }>("/admin/allow-list").then((r) => r.entries);
  }

  addToAllowList(email: string, notes?: string): Promise<AllowListEntry> {
    return this.send<AllowListEntry>("POST", "/admin/allow-list", { email, notes });
  }

  removeFromAllowList(email: string): Promise<void> {
    return this.del(`/admin/allow-list/${encodeURIComponent(email)}`);
  }

  /**
   * Fetch the plaintext value of a single secret key. This is the one call that
   * returns a real secret value; it should be made only when a reveal grant is
   * active and the returned value kept in memory only (never cached or logged).
   * The rest of the client only ever handles masked keys.
   */
  async revealValue(namespace: string, path: string, env: string, key: string): Promise<string> {
    const body = await this.get<{ value: string }>(
      `/namespaces/${encodeURIComponent(namespace)}/secrets/${encodeURIComponent(path)}` +
        `/reveal?env=${encodeURIComponent(env)}&key=${encodeURIComponent(key)}`,
    );
    return body.value;
  }

  private async get<T>(path: string): Promise<T> {
    const resp = await this.transport(`${this.base}${path}`, {
      headers: { Accept: "application/json" },
    });
    if (!resp.ok) {
      throw new ApiError(resp.status, await this.errorText(resp));
    }
    return (await resp.json()) as T;
  }

  private async send<T>(method: "POST" | "PATCH", path: string, body: unknown): Promise<T> {
    const resp = await this.transport(`${this.base}${path}`, {
      method,
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      throw new ApiError(resp.status, await this.errorText(resp));
    }
    return (await resp.json()) as T;
  }

  private async del(path: string): Promise<void> {
    const resp = await this.transport(`${this.base}${path}`, { method: "DELETE" });
    if (!resp.ok) {
      throw new ApiError(resp.status, await this.errorText(resp));
    }
  }

  private async errorText(resp: Response): Promise<string> {
    // The backend answers {"error": "..."} and its messages name the namespace,
    // path or field at fault, so they are worth showing. `detail` is the
    // framework's own shape, kept as a fallback.
    try {
      const data = await resp.json();
      if (typeof data?.error === "string") return data.error;
      if (typeof data?.detail === "string") return data.detail;
      return resp.statusText;
    } catch {
      return resp.statusText;
    }
  }
}

/** The default client, used by the app. Tests construct their own with a fake transport. */
export const api = new ApiClient();
