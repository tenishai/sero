<script lang="ts">
  import { branding } from "../branding";
  import { requestNamespaceMutation } from "$lib/queries";
  import type { NamespaceRequestResult } from "$lib/api";
  import Spinner from "$components/Spinner.svelte";

  // Self-service onboarding: request a new team namespace. This records a pending
  // request the platform admins action; it does not create the namespace itself.
  let teamName = $state("");
  let chosenEnvs = $state<Record<string, boolean>>(
    Object.fromEntries(branding.environments.map((e) => [e, e === branding.environments[0]])),
  );
  let membersText = $state("");
  let description = $state("");

  let error = $state<string | null>(null);
  let result = $state<NamespaceRequestResult | null>(null);

  const mut = requestNamespaceMutation();

  // Lowercase DNS-label style, matching the backend's team-name rule.
  const NAME_RE = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/;

  function members(): string[] {
    return membersText
      .split(/[\n,]/)
      .map((m) => m.trim())
      .filter(Boolean);
  }

  function selectedEnvs(): string[] {
    return branding.environments.filter((e) => chosenEnvs[e]);
  }

  async function submit() {
    error = null;
    result = null;
    const name = teamName.trim();
    if (!NAME_RE.test(name)) {
      error = "Team name must be lowercase letters, numbers, and hyphens (a DNS label).";
      return;
    }
    if (selectedEnvs().length === 0) {
      error = "Choose at least one environment.";
      return;
    }
    try {
      result = await mut.mutateAsync({
        teamName: name,
        environments: selectedEnvs(),
        members: members(),
        description: description.trim() || undefined,
      });
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }
</script>

<section class="max-w-xl">
  <h2 class="mb-1 text-lg font-semibold text-[var(--color-ink)]">Request a namespace</h2>
  <p class="mb-4 text-sm text-[var(--color-ink-muted)]">
    Ask for a new team namespace. Your request is recorded for a platform admin to
    review; you will get access once it is approved.
  </p>

  {#if result}
    <div class="rounded-md border border-[var(--color-success)] bg-[var(--color-card)] p-4 text-sm">
      <div class="font-semibold text-[var(--color-ink)]">Request submitted</div>
      <div class="mt-1 text-[var(--color-ink-muted)]">
        <span class="font-mono">{result.team_name}</span> &middot; request #{result.request_id}
        &middot; status <span class="font-mono">{result.status}</span>
      </div>
      <button
        type="button"
        class="mt-3 text-xs text-[var(--color-accent)]"
        onclick={() => {
          result = null;
          teamName = "";
          membersText = "";
          description = "";
        }}>Request another</button
      >
    </div>
  {:else}
    <div class="flex flex-col gap-4">
      <label class="flex flex-col gap-1">
        <span class="text-xs font-medium text-[var(--color-ink-muted)]">Team name</span>
        <input
          class="rounded bg-[var(--color-panel)] px-2.5 py-1.5 font-mono text-sm
                 text-[var(--color-ink)] ring-1 ring-[var(--color-border)] focus:outline-none
                 focus:ring-[var(--color-accent)]"
          placeholder="team-payments"
          bind:value={teamName}
        />
      </label>

      <fieldset class="flex flex-col gap-1">
        <span class="text-xs font-medium text-[var(--color-ink-muted)]">Environments</span>
        <div class="flex gap-3">
          {#each branding.environments as e (e)}
            <label class="flex items-center gap-1.5 text-sm text-[var(--color-ink)]">
              <input type="checkbox" bind:checked={chosenEnvs[e]} />
              {e}
            </label>
          {/each}
        </div>
      </fieldset>

      <label class="flex flex-col gap-1">
        <span class="text-xs font-medium text-[var(--color-ink-muted)]">
          Members (one email per line; leave blank for just you)
        </span>
        <textarea
          class="min-h-20 rounded bg-[var(--color-panel)] px-2.5 py-1.5 font-mono text-sm
                 text-[var(--color-ink)] ring-1 ring-[var(--color-border)] focus:outline-none
                 focus:ring-[var(--color-accent)]"
          placeholder="alice@example.com&#10;bob@example.com"
          bind:value={membersText}
        ></textarea>
      </label>

      <label class="flex flex-col gap-1">
        <span class="text-xs font-medium text-[var(--color-ink-muted)]">Description (optional)</span>
        <textarea
          class="min-h-16 rounded bg-[var(--color-panel)] px-2.5 py-1.5 text-sm
                 text-[var(--color-ink)] ring-1 ring-[var(--color-border)] focus:outline-none
                 focus:ring-[var(--color-accent)]"
          placeholder="What this team needs secrets for."
          bind:value={description}
        ></textarea>
      </label>

      {#if error}
        <p class="text-xs text-[var(--color-danger)]">{error}</p>
      {/if}

      <div class="flex items-center gap-2">
        {#if mut.isPending}
          <Spinner size="sm" />
        {/if}
        <button
          type="button"
          class="rounded bg-[var(--color-accent)] px-3 py-1.5 text-xs font-medium
                 text-[var(--color-on-accent)] disabled:opacity-60"
          disabled={mut.isPending}
          onclick={submit}>Submit request</button
        >
      </div>
    </div>
  {/if}
</section>
