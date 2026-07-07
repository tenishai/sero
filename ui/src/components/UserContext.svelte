<script lang="ts">
  import { meQuery } from "$lib/queries";
  import { isAdmin } from "$lib/access";
  import Spinner from "$components/Spinner.svelte";

  // Who is signed in. The identity comes from the reverse proxy (see the auth
  // model: no login form); this only displays it. Admin is derived from the
  // configured admin namespace, not a backend flag.
  const query = meQuery();
</script>

<div class="rounded-md border border-[var(--color-border)] bg-[var(--color-card)] p-2.5 text-xs">
  {#if query.isPending}
    <Spinner size="sm" />
  {:else if query.isError}
    <span class="text-[var(--color-ink-faint)]">Not signed in</span>
  {:else}
    <div class="flex items-center justify-between gap-2">
      <span class="truncate text-[var(--color-ink)]" title={query.data.email}>
        {query.data.email}
      </span>
      {#if isAdmin(query.data)}
        <span
          class="shrink-0 rounded bg-[var(--color-accent)] px-1.5 py-0.5 text-[10px]
                 font-medium text-[var(--color-on-accent)]"
        >
          admin
        </span>
      {/if}
    </div>
    <div class="mt-0.5 text-[var(--color-ink-faint)]">
      {query.data.namespaces.length}
      {query.data.namespaces.length === 1 ? "namespace" : "namespaces"}
    </div>
  {/if}
</div>
