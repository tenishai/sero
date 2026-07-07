<script lang="ts">
  // A minimal loading spinner. Token-styled so it inherits the active brand.
  let { size = "md" }: { size?: "sm" | "md" } = $props();
  const px = $derived(size === "sm" ? "h-4 w-4" : "h-6 w-6");
</script>

<span
  class="inline-block animate-spin rounded-full border-2 border-[var(--color-border)] border-t-[var(--color-accent)] {px}"
  role="status"
  aria-label="Loading"
></span>
