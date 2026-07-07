<script lang="ts">
  import { secretsQuery } from "$lib/queries";
  import { router } from "$lib/router.svelte";
  import Spinner from "$components/Spinner.svelte";

  let { namespace }: { namespace: string } = $props();

  // The secret paths in a namespace: names and key counts only, never values.
  const query = $derived(secretsQuery(namespace));
</script>

<section>
  <button
    type="button"
    class="mb-3 text-xs text-[var(--color-ink-muted)] hover:text-[var(--color-accent)]"
    onclick={() => router.go({ name: "namespaces" })}
  >
    &larr; Namespaces
  </button>
  <h2 class="mb-4 text-lg font-semibold text-[var(--color-ink)]">
    {namespace}
  </h2>

  {#if query.isPending}
    <Spinner />
  {:else if query.isError}
    <p class="text-sm text-[var(--color-danger)]">
      Could not load secrets: {query.error.message}
    </p>
  {:else if query.data.length === 0}
    <p class="text-sm text-[var(--color-ink-muted)]">No secrets in this namespace yet.</p>
  {:else}
    <ul class="flex flex-col gap-1">
      {#each query.data as secret (secret.path)}
        <li>
          <button
            type="button"
            class="flex w-full items-center justify-between rounded-md border
                   border-[var(--color-border)] bg-[var(--color-card)] px-3 py-2 text-left
                   text-sm text-[var(--color-ink)] transition-colors
                   hover:border-[var(--color-accent)]"
            onclick={() => router.go({ name: "secret", namespace, path: secret.path })}
          >
            <span class="font-mono">{secret.path}</span>
            <span class="text-xs text-[var(--color-ink-faint)]">
              {secret.key_count}
              {secret.key_count === 1 ? "key" : "keys"}
            </span>
          </button>
        </li>
      {/each}
    </ul>
  {/if}
</section>
