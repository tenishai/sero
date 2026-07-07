/**
 * A tiny hash router (Svelte 5 runes), generalized from the source UI.
 *
 * No router library: a `$state` current route, parse/build helpers, and a
 * `hashchange` listener. Routes are a small closed set; add to `Route` as views
 * land. Hash routing keeps the app a static SPA with no server-side routing.
 */

export type Route =
  | { name: "namespaces" }
  | { name: "request" }
  | { name: "secrets"; namespace: string }
  | { name: "secret"; namespace: string; path: string };

function parse(hash: string): Route {
  const raw = hash.replace(/^#\/?/, "");
  const parts = raw.split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "request") {
    return { name: "request" };
  }
  if (parts[0] === "ns" && parts[1]) {
    if (parts[2] === "secret" && parts[3]) {
      return { name: "secret", namespace: parts[1], path: parts.slice(3).join("/") };
    }
    return { name: "secrets", namespace: parts[1] };
  }
  return { name: "namespaces" };
}

export function toHash(route: Route): string {
  switch (route.name) {
    case "namespaces":
      return "#/";
    case "request":
      return "#/request";
    case "secrets":
      return `#/ns/${encodeURIComponent(route.namespace)}`;
    case "secret":
      return (
        `#/ns/${encodeURIComponent(route.namespace)}/secret/` +
        route.path.split("/").map(encodeURIComponent).join("/")
      );
  }
}

class RouterState {
  current = $state<Route>({ name: "namespaces" });

  init(): void {
    this.current = parse(location.hash);
    addEventListener("hashchange", () => {
      this.current = parse(location.hash);
    });
  }

  go(route: Route): void {
    location.hash = toHash(route);
  }
}

export const router = new RouterState();
