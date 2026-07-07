/**
 * Typed API client for the Ladon backend.
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

const DEFAULT_BASE = "/api";

export class ApiClient {
  private readonly transport: Transport;
  private readonly base: string;

  constructor(transport: Transport = fetch.bind(globalThis), base: string = DEFAULT_BASE) {
    this.transport = transport;
    this.base = base;
  }

  async listNamespaces(): Promise<string[]> {
    const body = await this.get<NamespacesResponse>("/namespaces");
    return body.namespaces;
  }

  async listSecrets(namespace: string): Promise<SecretPath[]> {
    const body = await this.get<SecretsResponse>(
      `/namespaces/${encodeURIComponent(namespace)}/secrets`,
    );
    return body.secrets;
  }

  async getSecret(namespace: string, path: string, env: string): Promise<SecretDetail> {
    return this.get<SecretDetail>(
      `/namespaces/${encodeURIComponent(namespace)}/secrets/${encodeURIComponent(path)}` +
        `?env=${encodeURIComponent(env)}`,
    );
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

  private async errorText(resp: Response): Promise<string> {
    try {
      const data = await resp.json();
      return typeof data?.detail === "string" ? data.detail : resp.statusText;
    } catch {
      return resp.statusText;
    }
  }
}

/** The default client, used by the app. Tests construct their own with a fake transport. */
export const api = new ApiClient();
