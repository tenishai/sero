<script lang="ts">
  import { branding } from "../branding";
  import { secretQuery } from "$lib/queries";
  import { reveal } from "$lib/reveal.svelte";
  import { router } from "$lib/router.svelte";
  import Spinner from "$components/Spinner.svelte";
  import SecretEditor from "$views/SecretEditor.svelte";

  let { namespace, path }: { namespace: string; path: string } = $props();

  // One secret path, per environment. Keys are shown by NAME with a masked
  // preview by default. Revealing a value is an explicit, per-key action that
  // fetches the plaintext on demand; the value is held only in this component's
  // memory (revealed map below) and cleared when hidden or the env changes.
  let env = $state(branding.environments[0]);
  let editing = $state(false);
  // key -> revealed plaintext, in-memory only. Never persisted or logged.
  let revealed = $state<Record<string, string>>({});
  let revealing = $state<string | null>(null);
  let revealError = $state<string | null>(null);
  const query = $derived(secretQuery(namespace, path, env));

  function resetReveals() {
    revealed = {};
    revealError = null;
  }

  async function revealKey(key: string) {
    revealError = null;
    revealing = key;
    try {
      const value = await reveal.revealValue(namespace, path, env, key);
      revealed = { ...revealed, [key]: value };
    } catch (e) {
      revealError = e instanceof Error ? e.message : String(e);
    } finally {
      revealing = null;
    }
  }

  function hideKey(key: string) {
    const { [key]: _dropped, ...rest } = revealed;
    revealed = rest;
  }

  async function copyKey(key: string) {
    if (revealed[key] !== undefined) await navigator.clipboard.writeText(revealed[key]);
  }
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
        onclick={() => {
          env = e;
          editing = false;
          resetReveals();
        }}
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
          <th class="py-1"></th>
        </tr>
      </thead>
      <tbody>
        {#each Object.entries(query.data.masked_keys) as [key, masked] (key)}
          {@const shown = revealed[key] !== undefined}
          <tr class="border-t border-[var(--color-border)]">
            <td class="py-1.5 pr-4 align-top font-mono text-[var(--color-ink)]">{key}</td>
            <td class="w-full py-1.5 font-mono">
              {#if shown}
                <span class="break-all text-[var(--color-ink)]">{revealed[key]}</span>
              {:else}
                <span class="text-[var(--color-ink-faint)]" aria-label="masked value">{masked}</span>
              {/if}
            </td>
            <td class="py-1.5 pl-3 text-right align-top whitespace-nowrap">
              {#if revealing === key}
                <Spinner size="sm" />
              {:else if shown}
                <button
                  type="button"
                  class="text-xs text-[var(--color-ink-muted)] hover:text-[var(--color-accent)]"
                  onclick={() => copyKey(key)}>copy</button
                >
                <button
                  type="button"
                  class="ml-2 text-xs text-[var(--color-ink-muted)] hover:text-[var(--color-accent)]"
                  onclick={() => hideKey(key)}>hide</button
                >
              {:else}
                <button
                  type="button"
                  class="text-xs text-[var(--color-accent)] hover:text-[var(--color-accent-strong)]"
                  onclick={() => revealKey(key)}>reveal</button
                >
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
    {#if revealError}
      <p class="mt-2 text-xs text-[var(--color-danger)]">{revealError}</p>
    {/if}
    <p class="mt-3 text-xs text-[var(--color-ink-faint)]">
      Values are masked by default. Revealing fetches a value on demand; it is
      shown only here and never stored.
    </p>
  {/if}

  {#if !query.isPending && !query.isError}
    <div class="mt-4">
      {#if editing}
        {#key env}
          <SecretEditor
            {namespace}
            {path}
            {env}
            mode={query.data.exists ? "update" : "create"}
            existingKeys={Object.keys(query.data.masked_keys)}
            ondone={() => (editing = false)}
          />
        {/key}
      {:else}
        <button
          type="button"
          class="rounded border border-[var(--color-border)] px-3 py-1.5 text-xs
                 text-[var(--color-ink)] hover:border-[var(--color-accent)]"
          onclick={() => (editing = true)}
        >
          {query.data.exists ? "Edit" : "Create in this environment"}
        </button>
      {/if}
    </div>
  {/if}
</section>
