<script lang="ts">
  import { branding } from "./branding";
  import ThemeToggle from "$components/ThemeToggle.svelte";

  // The app shell. Its identity (wordmark, tagline) comes entirely from
  // branding.ts, so rebranding never edits this file. This first slice is the
  // shell and the rebrand contract; views land in following slices.
</script>

<div class="flex min-h-screen">
  <aside
    class="flex w-60 flex-col border-r border-[var(--color-border)] bg-[var(--color-panel)] p-4"
  >
    <div class="mb-8">
      <div class="text-lg font-semibold text-[var(--color-ink)]">{branding.productName}</div>
      {#if branding.tagline}
        <div class="text-xs text-[var(--color-ink-faint)]">{branding.tagline}</div>
      {/if}
    </div>

    <nav class="flex flex-col gap-1 text-sm text-[var(--color-ink-muted)]">
      <!-- Navigation lands with the views; the shell proves the layout + theme. -->
      <span class="rounded px-2 py-1.5 text-[var(--color-ink-faint)]">Namespaces</span>
      <span class="rounded px-2 py-1.5 text-[var(--color-ink-faint)]">Admin</span>
    </nav>

    <div class="mt-auto pt-4">
      <ThemeToggle />
    </div>
  </aside>

  <main class="flex-1 p-8">
    <h1 class="text-xl font-semibold text-[var(--color-ink)]">{branding.productName}</h1>
    <p class="mt-2 max-w-prose text-sm text-[var(--color-ink-muted)]">
      Operator interface for the secrets platform. This is the UI foundation: the
      theme system and the single-file rebrand contract. Namespace, secret, and
      admin views follow.
    </p>
    <p class="mt-4 text-sm text-[var(--color-accent)]">Accent color sample</p>
  </main>
</div>
