<script lang="ts">
  import { namespacesQuery } from "$lib/queries";
  import { router } from "$lib/router.svelte";
  import Spinner from "$components/Spinner.svelte";

  // The landing view: the namespaces the signed-in user may access. Selecting
  // one navigates to its secrets list.
  const query = namespacesQuery();
</script>

<section>
  <h2 class="mb-4 text-lg font-semibold text-[var(--color-ink)]">Namespaces</h2>

  {#if query.isPending}
    <Spinner />
  {:else if query.isError}
    <p class="text-sm text-[var(--color-danger)]">
      Could not load namespaces: {query.error.message}
    </p>
  {:else if query.data.length === 0}
    <p class="text-sm text-[var(--color-ink-muted)]">
      You do not have access to any namespaces yet.
    </p>
  {:else}
    <ul class="flex flex-col gap-1">
      {#each query.data as ns (ns)}
        <li>
          <button
            type="button"
            class="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-card)]
                   px-3 py-2 text-left text-sm text-[var(--color-ink)] transition-colors
                   hover:border-[var(--color-accent)]"
            onclick={() => router.go({ name: "secrets", namespace: ns })}
          >
            {ns}
          </button>
        </li>
      {/each}
    </ul>
  {/if}
</section>
