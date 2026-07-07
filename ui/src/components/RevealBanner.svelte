<script lang="ts">
  import { reveal } from "$lib/reveal.svelte";

  // Shows the time left on an EXPIRING reveal grant, with a revoke action. With
  // the always-allow default gate there is no expiry, so this renders nothing;
  // it appears only when a deployment plugs in a time-boxed step-up gate.
  const show = $derived(reveal.active && reveal.expiresAt !== null);

  function mmss(total: number): string {
    if (!isFinite(total)) return "";
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${s.toString().padStart(2, "0")}`;
  }
</script>

{#if show}
  <div
    class="flex items-center justify-between gap-2 rounded-md border border-[var(--color-accent)]
           bg-[var(--color-card)] px-3 py-1.5 text-xs"
  >
    <span class="text-[var(--color-ink)]">
      Reveal unlocked &middot; {mmss(reveal.secondsLeft)} left
    </span>
    <button
      type="button"
      class="text-[var(--color-ink-muted)] hover:text-[var(--color-danger)]"
      onclick={() => reveal.revoke()}>lock</button
    >
  </div>
{/if}
