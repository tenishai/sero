<script lang="ts">
  import { QueryClient, QueryClientProvider } from "@tanstack/svelte-query";
  import { branding } from "./branding";
  import { router } from "$lib/router.svelte";
  import ThemeToggle from "$components/ThemeToggle.svelte";
  import UserContext from "$components/UserContext.svelte";
  import Namespaces from "$views/Namespaces.svelte";
  import Secrets from "$views/Secrets.svelte";
  import SecretDetail from "$views/SecretDetail.svelte";
  import RequestNamespace from "$views/RequestNamespace.svelte";

  const nav = [
    { label: "Namespaces", route: { name: "namespaces" } as const },
    { label: "Request namespace", route: { name: "request" } as const },
  ];
  const activeTop = $derived(router.current.name === "request" ? "request" : "namespaces");

  // The app shell: identity from branding.ts, routing from the hash router, and
  // a QueryClient for the server-state layer. Rebranding never edits this file.
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: 1, staleTime: 15_000 } },
  });
</script>

<QueryClientProvider client={queryClient}>
  <div class="flex min-h-screen">
    <aside
      class="flex w-60 flex-col border-r border-[var(--color-border)] bg-[var(--color-panel)] p-4"
    >
      <button
        type="button"
        class="mb-8 text-left"
        onclick={() => router.go({ name: "namespaces" })}
      >
        <div class="text-lg font-semibold text-[var(--color-ink)]">{branding.productName}</div>
        {#if branding.tagline}
          <div class="text-xs text-[var(--color-ink-faint)]">{branding.tagline}</div>
        {/if}
      </button>

      <nav class="flex flex-col gap-0.5 text-sm">
        {#each nav as item (item.label)}
          <button
            type="button"
            class="rounded px-2 py-1.5 text-left transition-colors"
            class:bg-[var(--color-card)]={activeTop === item.route.name}
            class:text-[var(--color-ink)]={activeTop === item.route.name}
            class:text-[var(--color-ink-muted)]={activeTop !== item.route.name}
            onclick={() => router.go(item.route)}
          >
            {item.label}
          </button>
        {/each}
      </nav>

      <div class="mt-auto flex flex-col gap-3 pt-4">
        <UserContext />
        <ThemeToggle />
      </div>
    </aside>

    <main class="flex-1 p-8">
      {#if router.current.name === "namespaces"}
        <Namespaces />
      {:else if router.current.name === "request"}
        <RequestNamespace />
      {:else if router.current.name === "secrets"}
        <Secrets namespace={router.current.namespace} />
      {:else if router.current.name === "secret"}
        <SecretDetail namespace={router.current.namespace} path={router.current.path} />
      {/if}
    </main>
  </div>
</QueryClientProvider>
