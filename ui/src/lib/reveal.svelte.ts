/**
 * Reveal store (Svelte 5 runes): the grant state and the gated plaintext fetch.
 *
 * Holds whether a reveal grant is active and, if it expires, a live seconds-
 * remaining countdown. `ensureActive()` and `revoke()` delegate to the injected
 * RevealGate (default: always-allow). `revealValue()` ensures a grant, then
 * fetches one key's plaintext through the API client.
 *
 * Hygiene: plaintext returned by revealValue is handed straight to the caller and
 * kept in the component's memory only. This store never stores, caches, or logs a
 * value; it only tracks the grant. A lapsed grant flips active to false so the UI
 * re-hides.
 */

import { api, ApiClient } from "./api";
import { AlwaysAllowGate, type RevealGate, type RevealGrant } from "./revealGate";

class RevealStore {
  active = $state(false);
  expiresAt = $state<number | null>(null);
  private nowTick = $state(Date.now());

  private gate: RevealGate = new AlwaysAllowGate();
  private client: ApiClient = api;
  private timer: ReturnType<typeof setInterval> | null = null;

  /** Configure the gate and client (a deployment injects a stricter gate). */
  configure(opts: { gate?: RevealGate; client?: ApiClient }): void {
    if (opts.gate) this.gate = opts.gate;
    if (opts.client) this.client = opts.client;
  }

  /** Seconds left on an expiring grant; 0 when none or lapsed. Null-expiry = Infinity. */
  get secondsLeft(): number {
    if (!this.active) return 0;
    if (this.expiresAt === null) return Infinity;
    return Math.max(0, Math.floor((this.expiresAt - this.nowTick) / 1000));
  }

  async hydrate(): Promise<void> {
    this.apply(await this.gate.status());
  }

  async ensureActive(): Promise<void> {
    this.apply(await this.gate.ensureActive());
  }

  async revoke(): Promise<void> {
    await this.gate.revoke();
    this.apply({ active: false, expiresAt: null });
  }

  /** Reveal one key's plaintext. Ensures a grant first. Value is not retained here. */
  async revealValue(namespace: string, path: string, env: string, key: string): Promise<string> {
    if (!this.active) await this.ensureActive();
    return this.client.revealValue(namespace, path, env, key);
  }

  private apply(grant: RevealGrant): void {
    this.active = grant.active;
    this.expiresAt = grant.active ? grant.expiresAt : null;
    this.manageTimer();
  }

  private manageTimer(): void {
    const needsTimer = this.active && this.expiresAt !== null;
    if (needsTimer && this.timer === null) {
      this.timer = setInterval(() => {
        this.nowTick = Date.now();
        if (this.expiresAt !== null && this.nowTick >= this.expiresAt) {
          this.apply({ active: false, expiresAt: null });
        }
      }, 1000);
    } else if (!needsTimer && this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }
}

export const reveal = new RevealStore();
