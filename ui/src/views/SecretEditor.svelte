<script lang="ts">
  import { createSecretMutation, updateSecretMutation, type WriteVars } from "$lib/queries";
  import { placeholderKeys } from "$lib/hygiene";
  import type { WriteSummary } from "$lib/api";
  import Spinner from "$components/Spinner.svelte";

  // Edit a secret path with the backend's dry-run-first contract surfaced as a
  // two-step: enter key/value rows -> Review (dry-run, apply=false, shows a
  // value-free summary) -> Apply (commit, apply=true). For an existing path this
  // MERGES: a blank value on an existing key means "leave as-is"; removal is
  // explicit. Values are never echoed back after entry.
  let {
    namespace,
    path,
    env,
    mode,
    existingKeys = [],
    ondone,
  }: {
    namespace: string;
    path: string;
    env: string;
    mode: "create" | "update";
    /** Existing key names (update mode), so the user can target or remove them. */
    existingKeys?: string[];
    ondone?: () => void;
  } = $props();

  type Row = { key: string; value: string; remove: boolean; existing: boolean };

  // Rows are initialized once from the props (the parent remounts this editor
  // per environment via {#key env}), so this deliberately captures initial
  // values and is not reactive.
  function initialRows(): Row[] {
    if (mode === "update") {
      return existingKeys.map((k) => ({ key: k, value: "", remove: false, existing: true }));
    }
    return [{ key: "", value: "", remove: false, existing: false }];
  }

  let rows = $state<Row[]>(initialRows());

  let preview = $state<WriteSummary | null>(null);
  let error = $state<string | null>(null);

  const createMut = createSecretMutation();
  const updateMut = updateSecretMutation();

  function addRow() {
    rows = [...rows, { key: "", value: "", remove: false, existing: false }];
    preview = null;
  }

  function dropRow(i: number) {
    rows = rows.filter((_, idx) => idx !== i);
    preview = null;
  }

  // The values to write: rows with a non-empty value and a key. A blank value on
  // an existing key is skipped (leave as-is); a new row needs both key and value.
  function collect(): { values: Record<string, string>; removeKeys: string[] } {
    const values: Record<string, string> = {};
    const removeKeys: string[] = [];
    for (const row of rows) {
      const key = row.key.trim();
      if (!key) continue;
      if (row.remove && row.existing) {
        removeKeys.push(key);
        continue;
      }
      if (row.value !== "") values[key] = row.value;
    }
    return { values, removeKeys };
  }

  function vars(apply: boolean): WriteVars {
    const { values, removeKeys } = collect();
    return { namespace, path, env, values, removeKeys, apply };
  }

  async function review() {
    error = null;
    preview = null;
    const { values, removeKeys } = collect();
    const bad = placeholderKeys(values);
    if (bad.length) {
      error = `These look like placeholder values, not real secrets: ${bad.join(", ")}. ` +
        "Enter real values or clear them.";
      return;
    }
    if (Object.keys(values).length === 0 && removeKeys.length === 0) {
      error = "Nothing to change: enter a value or mark a key for removal.";
      return;
    }
    try {
      const mut = mode === "create" ? createMut : updateMut;
      preview = await mut.mutateAsync(vars(false));
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  async function apply() {
    error = null;
    try {
      const mut = mode === "create" ? createMut : updateMut;
      await mut.mutateAsync(vars(true));
      ondone?.();
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  const busy = $derived(createMut.isPending || updateMut.isPending);
</script>

<div class="rounded-md border border-[var(--color-border)] bg-[var(--color-card)] p-4">
  <h3 class="mb-3 text-sm font-semibold text-[var(--color-ink)]">
    {mode === "create" ? "Create secret" : "Edit secret"} in
    <span class="font-mono">{env}</span>
  </h3>

  <div class="flex flex-col gap-2">
    {#each rows as row, i (i)}
      <div class="flex items-center gap-2">
        <input
          class="w-1/3 rounded bg-[var(--color-panel)] px-2 py-1.5 font-mono text-xs
                 text-[var(--color-ink)] ring-1 ring-[var(--color-border)] focus:outline-none
                 focus:ring-[var(--color-accent)] disabled:opacity-60"
          placeholder="KEY_NAME"
          bind:value={row.key}
          disabled={row.existing}
          oninput={() => (preview = null)}
        />
        <input
          class="flex-1 rounded bg-[var(--color-panel)] px-2 py-1.5 font-mono text-xs
                 text-[var(--color-ink)] ring-1 ring-[var(--color-border)] focus:outline-none
                 focus:ring-[var(--color-accent)]"
          type="password"
          placeholder={row.existing ? "(leave blank to keep)" : "value"}
          bind:value={row.value}
          disabled={row.remove}
          oninput={() => (preview = null)}
        />
        {#if row.existing}
          <label class="flex items-center gap-1 text-xs text-[var(--color-ink-muted)]">
            <input type="checkbox" bind:checked={row.remove} onchange={() => (preview = null)} />
            remove
          </label>
        {:else}
          <button
            type="button"
            class="text-xs text-[var(--color-ink-faint)] hover:text-[var(--color-danger)]"
            onclick={() => dropRow(i)}>drop</button
          >
        {/if}
      </div>
    {/each}
  </div>

  <button
    type="button"
    class="mt-2 text-xs text-[var(--color-accent)] hover:text-[var(--color-accent-strong)]"
    onclick={addRow}>+ add key</button
  >

  {#if error}
    <p class="mt-3 text-xs text-[var(--color-danger)]">{error}</p>
  {/if}

  {#if preview}
    <div class="mt-3 rounded border border-[var(--color-border)] bg-[var(--color-panel)] p-3 text-xs">
      <div class="mb-1 font-semibold text-[var(--color-ink)]">
        Dry run &mdash; nothing written yet
      </div>
      <div class="text-[var(--color-ink-muted)]">
        {preview.action} <span class="font-mono">{preview.path}</span> in
        <span class="font-mono">{preview.env}</span>
      </div>
      {#if preview.written_keys.length}
        <div class="mt-1 text-[var(--color-ink-muted)]">
          write keys: <span class="font-mono text-[var(--color-ink)]"
            >{preview.written_keys.join(", ")}</span
          >
        </div>
      {/if}
      {#if preview.removed_keys.length}
        <div class="mt-1 text-[var(--color-ink-muted)]">
          remove keys: <span class="font-mono text-[var(--color-danger)]"
            >{preview.removed_keys.join(", ")}</span
          >
        </div>
      {/if}
    </div>
  {/if}

  <div class="mt-4 flex items-center gap-2">
    {#if busy}
      <Spinner size="sm" />
    {/if}
    {#if !preview}
      <button
        type="button"
        class="rounded bg-[var(--color-accent)] px-3 py-1.5 text-xs font-medium
               text-[var(--color-on-accent)] disabled:opacity-60"
        disabled={busy}
        onclick={review}>Review changes</button
      >
    {:else}
      <button
        type="button"
        class="rounded bg-[var(--color-accent)] px-3 py-1.5 text-xs font-medium
               text-[var(--color-on-accent)] disabled:opacity-60"
        disabled={busy}
        onclick={apply}>Apply</button
      >
      <button
        type="button"
        class="rounded px-3 py-1.5 text-xs text-[var(--color-ink-muted)]"
        disabled={busy}
        onclick={() => (preview = null)}>Back</button
      >
    {/if}
    {#if ondone}
      <button
        type="button"
        class="ml-auto text-xs text-[var(--color-ink-faint)]"
        onclick={ondone}>Close</button
      >
    {/if}
  </div>
</div>
