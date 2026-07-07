/**
 * Server-state query layer (TanStack Query).
 *
 * Thin `createQuery` factories over the API client, so views declare what data
 * they need and get loading/error/data for free. The client is module-level and
 * injectable via `setApiClient` (tests point it at a fake transport), keeping the
 * views free of transport concerns.
 */

import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
import { ApiClient, api, type NamespaceRequestResult, type WriteSummary } from "./api";

let client: ApiClient = api;

/** Override the client (tests/previews inject one backed by a fake transport). */
export function setApiClient(next: ApiClient): void {
  client = next;
}

// createQuery takes an Accessor (a function returning options) in the Svelte 5
// runes API, and returns a reactive result accessed directly (query.data,
// query.isPending), not a store.
export function meQuery() {
  return createQuery(() => ({
    queryKey: ["me"],
    queryFn: () => client.getMe(),
    staleTime: 5 * 60_000, // identity rarely changes within a session
  }));
}

export function namespacesQuery() {
  return createQuery(() => ({
    queryKey: ["namespaces"],
    queryFn: () => client.listNamespaces(),
  }));
}

export function secretsQuery(namespace: string) {
  return createQuery(() => ({
    queryKey: ["secrets", namespace],
    queryFn: () => client.listSecrets(namespace),
  }));
}

export function secretQuery(namespace: string, path: string, env: string) {
  return createQuery(() => ({
    queryKey: ["secret", namespace, path, env],
    queryFn: () => client.getSecret(namespace, path, env),
  }));
}

export interface WriteVars {
  namespace: string;
  path: string;
  env: string;
  values: Record<string, string>;
  /** For update: keys to delete. Ignored for create. */
  removeKeys?: string[];
  /** false = dry-run (default), true = commit. */
  apply?: boolean;
}

export function createSecretMutation() {
  const qc = useQueryClient();
  return createMutation(() => ({
    mutationFn: (v: WriteVars): Promise<WriteSummary> =>
      client.createSecret(v.namespace, v.path, v.env, v.values, v.apply ?? false),
    onSuccess: (result, v) => {
      // Only a committed write changes server state worth refetching.
      if (result.applied) invalidateSecret(qc, v);
    },
  }));
}

export function updateSecretMutation() {
  const qc = useQueryClient();
  return createMutation(() => ({
    mutationFn: (v: WriteVars): Promise<WriteSummary> =>
      client.updateSecret(v.namespace, v.path, v.env, v.values, v.removeKeys ?? [], v.apply ?? false),
    onSuccess: (result, v) => {
      if (result.applied) invalidateSecret(qc, v);
    },
  }));
}

export interface NamespaceRequestVars {
  teamName: string;
  environments: string[];
  members: string[];
  description?: string;
}

export function requestNamespaceMutation() {
  return createMutation(() => ({
    mutationFn: (v: NamespaceRequestVars): Promise<NamespaceRequestResult> =>
      client.requestNamespace(v.teamName, v.environments, v.members, v.description),
  }));
}

function invalidateSecret(qc: ReturnType<typeof useQueryClient>, v: WriteVars): void {
  qc.invalidateQueries({ queryKey: ["secret", v.namespace, v.path, v.env] });
  qc.invalidateQueries({ queryKey: ["secrets", v.namespace] });
}
