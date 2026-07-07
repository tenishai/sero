/**
 * Server-state query layer (TanStack Query).
 *
 * Thin `createQuery` factories over the API client, so views declare what data
 * they need and get loading/error/data for free. The client is module-level and
 * injectable via `setApiClient` (tests point it at a fake transport), keeping the
 * views free of transport concerns.
 */

import { createQuery } from "@tanstack/svelte-query";
import { ApiClient, api } from "./api";

let client: ApiClient = api;

/** Override the client (tests/previews inject one backed by a fake transport). */
export function setApiClient(next: ApiClient): void {
  client = next;
}

// createQuery takes an Accessor (a function returning options) in the Svelte 5
// runes API, and returns a reactive result accessed directly (query.data,
// query.isPending), not a store.
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
