<script lang="ts">
  import { branding } from "../branding";
  import { secretQuery } from "$lib/queries";
  import { router } from "$lib/router.svelte";
  import Spinner from "$components/Spinner.svelte";

  let { namespace, path }: { namespace: string; path: string } = $props();

  // One secret path, per environment. Keys are shown by NAME with a masked
  // preview only; a plaintext value is never shown here. Revealing a value is a
  // separate, explicit action (a later slice).
  let env = $state(branding.environments[0]);
  const query = $derived(secretQuery(namespace, path, env));
</script>

<section>
  <button
    type="button"
    class="mb-3 text-xs text-[var(--color-ink-muted)] hover:text-[var(--color-accent)]"
    onclick={() => router.go({ name: "secrets", namespace })}
  >
    &larr; {namespace}
  </button>
  <h2 class="mb-4 font-mono text-lg font-semibold text-[var(--color-ink)]">{path}</h2>

  <div class="mb-4 flex gap-1" role="radiogroup" aria-label="Environment">
    {#each branding.environments as e (e)}
      <button
        type="button"
        role="radio"
        aria-checked={env === e}
        class="rounded px-2.5 py-1 text-xs font-medium transition-colors"
        class:bg-[var(--color-accent)]={env === e}
        class:text-[var(--color-on-accent)]={env === e}
        class:text-[var(--color-ink-muted)]={env !== e}
        onclick={() => (env = e)}
      >
        {e}
      </button>
    {/each}
  </div>

  {#if query.isPending}
    <Spinner />
  {:else if query.isError}
    <p class="text-sm text-[var(--color-danger)]">
      Could not load secret: {query.error.message}
    </p>
  {:else if !query.data.exists}
    <p class="text-sm text-[var(--color-ink-muted)]">
      This path does not exist in <span class="font-mono">{env}</span>.
    </p>
  {:else}
    <div class="mb-2 text-xs text-[var(--color-ink-faint)]">
      version {query.data.version ?? "?"} &middot; {query.data.key_count}
      {query.data.key_count === 1 ? "key" : "keys"}
    </div>
    <table class="w-full text-sm">
      <thead>
        <tr class="text-left text-xs text-[var(--color-ink-faint)]">
          <th class="py-1 font-medium">Key</th>
          <th class="py-1 font-medium">Value</th>
        </tr>
      </thead>
      <tbody>
        {#each Object.entries(query.data.masked_keys) as [key, masked] (key)}
          <tr class="border-t border-[var(--color-border)]">
            <td class="py-1.5 pr-4 font-mono text-[var(--color-ink)]">{key}</td>
            <td class="py-1.5 font-mono text-[var(--color-ink-faint)]" aria-label="masked value">
              {masked}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
    <p class="mt-3 text-xs text-[var(--color-ink-faint)]">
      Values are masked. Revealing a value is an explicit action, coming in a
      later release.
    </p>
  {/if}
</section>
