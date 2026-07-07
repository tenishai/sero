<script lang="ts">
  import { theme } from "$lib/theme.svelte";

  // A small segmented Dark/Light control. Purely token-styled, so it inherits
  // whatever palette the active brand defines.
  const options: { value: "dark" | "light"; label: string }[] = [
    { value: "dark", label: "Dark" },
    { value: "light", label: "Light" },
  ];
</script>

<div
  class="inline-flex rounded-md border border-[var(--color-border)] bg-[var(--color-panel)] p-0.5"
  role="radiogroup"
  aria-label="Theme"
>
  {#each options as opt (opt.value)}
    <button
      type="button"
      role="radio"
      aria-checked={theme.current === opt.value}
      class="rounded px-2.5 py-1 text-xs font-medium transition-colors"
      class:bg-[var(--color-accent)]={theme.current === opt.value}
      class:text-[var(--color-on-accent)]={theme.current === opt.value}
      class:text-[var(--color-ink-muted)]={theme.current !== opt.value}
      onclick={() => theme.set(opt.value)}
    >
      {opt.label}
    </button>
  {/each}
</div>
