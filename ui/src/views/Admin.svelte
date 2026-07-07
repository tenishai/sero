<script lang="ts">
  import {
    meQuery,
    pendingRequestsQuery,
    allowListQuery,
    approveRequestMutation,
    rejectRequestMutation,
    addAllowMutation,
    removeAllowMutation,
  } from "$lib/queries";
  import { isAdmin } from "$lib/access";
  import Spinner from "$components/Spinner.svelte";

  // Admin panel: review namespace requests and manage the MCP allow list. Gated
  // in the UI by isAdmin (the backend enforces regardless); a non-admin who
  // reaches this route sees a not-authorized notice rather than the panel.
  const me = meQuery();
  const requests = pendingRequestsQuery();
  const allow = allowListQuery();

  const approve = approveRequestMutation();
  const reject = rejectRequestMutation();
  const addAllow = addAllowMutation();
  const removeAllow = removeAllowMutation();

  let newEmail = $state("");
  let newNotes = $state("");

  async function submitAllow() {
    const email = newEmail.trim();
    if (!email) return;
    await addAllow.mutateAsync({ email, notes: newNotes.trim() || undefined });
    newEmail = "";
    newNotes = "";
  }
</script>

<section class="max-w-3xl">
  <h2 class="mb-4 text-lg font-semibold text-[var(--color-ink)]">Admin</h2>

  {#if me.isPending}
    <Spinner />
  {:else if !isAdmin(me.data)}
    <p class="text-sm text-[var(--color-danger)]">
      You are not authorized to view the admin panel.
    </p>
  {:else}
    <!-- Pending namespace requests -->
    <div class="mb-8">
      <h3 class="mb-2 text-sm font-semibold text-[var(--color-ink)]">
        Pending namespace requests
      </h3>
      {#if requests.isPending}
        <Spinner size="sm" />
      {:else if requests.isError}
        <p class="text-sm text-[var(--color-danger)]">
          Could not load requests: {requests.error.message}
        </p>
      {:else if requests.data.length === 0}
        <p class="text-sm text-[var(--color-ink-muted)]">No pending requests.</p>
      {:else}
        <ul class="flex flex-col gap-2">
          {#each requests.data as req (req.id)}
            <li
              class="rounded-md border border-[var(--color-border)] bg-[var(--color-card)] p-3 text-sm"
            >
              <div class="flex items-start justify-between gap-3">
                <div>
                  <div class="font-mono text-[var(--color-ink)]">{req.team_name}</div>
                  <div class="text-xs text-[var(--color-ink-muted)]">
                    {req.requester_email} &middot; {req.environments.join(", ")} &middot;
                    {req.members.length} members
                  </div>
                  {#if req.description}
                    <div class="mt-1 text-xs text-[var(--color-ink-faint)]">{req.description}</div>
                  {/if}
                </div>
                <div class="flex shrink-0 gap-2">
                  <button
                    type="button"
                    class="rounded bg-[var(--color-accent)] px-2.5 py-1 text-xs font-medium
                           text-[var(--color-on-accent)] disabled:opacity-60"
                    disabled={approve.isPending}
                    onclick={() => approve.mutate(req.id)}>Approve</button
                  >
                  <button
                    type="button"
                    class="rounded px-2.5 py-1 text-xs text-[var(--color-danger)]
                           ring-1 ring-[var(--color-border)] disabled:opacity-60"
                    disabled={reject.isPending}
                    onclick={() => reject.mutate({ id: req.id, reason: "rejected by admin" })}
                    >Reject</button
                  >
                </div>
              </div>
            </li>
          {/each}
        </ul>
      {/if}
    </div>

    <!-- MCP allow list -->
    <div>
      <h3 class="mb-2 text-sm font-semibold text-[var(--color-ink)]">Agent allow list</h3>
      <p class="mb-2 text-xs text-[var(--color-ink-muted)]">
        Identities permitted to use the agent (MCP) surface.
      </p>

      <div class="mb-3 flex items-end gap-2">
        <label class="flex flex-1 flex-col gap-1">
          <span class="text-xs text-[var(--color-ink-muted)]">Email</span>
          <input
            class="rounded bg-[var(--color-panel)] px-2 py-1.5 text-xs text-[var(--color-ink)]
                   ring-1 ring-[var(--color-border)] focus:outline-none focus:ring-[var(--color-accent)]"
            placeholder="person@example.com"
            bind:value={newEmail}
          />
        </label>
        <label class="flex flex-1 flex-col gap-1">
          <span class="text-xs text-[var(--color-ink-muted)]">Notes (optional)</span>
          <input
            class="rounded bg-[var(--color-panel)] px-2 py-1.5 text-xs text-[var(--color-ink)]
                   ring-1 ring-[var(--color-border)] focus:outline-none focus:ring-[var(--color-accent)]"
            bind:value={newNotes}
          />
        </label>
        <button
          type="button"
          class="rounded bg-[var(--color-accent)] px-3 py-1.5 text-xs font-medium
                 text-[var(--color-on-accent)] disabled:opacity-60"
          disabled={addAllow.isPending || !newEmail.trim()}
          onclick={submitAllow}>Add</button
        >
      </div>

      {#if allow.isPending}
        <Spinner size="sm" />
      {:else if allow.isError}
        <p class="text-sm text-[var(--color-danger)]">
          Could not load allow list: {allow.error.message}
        </p>
      {:else if allow.data.length === 0}
        <p class="text-sm text-[var(--color-ink-muted)]">No one on the allow list yet.</p>
      {:else}
        <ul class="flex flex-col gap-1">
          {#each allow.data as entry (entry.email)}
            <li
              class="flex items-center justify-between rounded border border-[var(--color-border)]
                     bg-[var(--color-card)] px-3 py-1.5 text-sm"
            >
              <div>
                <span class="text-[var(--color-ink)]">{entry.email}</span>
                {#if entry.notes}
                  <span class="ml-2 text-xs text-[var(--color-ink-faint)]">{entry.notes}</span>
                {/if}
              </div>
              <button
                type="button"
                class="text-xs text-[var(--color-ink-faint)] hover:text-[var(--color-danger)]
                       disabled:opacity-60"
                disabled={removeAllow.isPending}
                onclick={() => removeAllow.mutate(entry.email)}>remove</button
              >
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  {/if}
</section>
