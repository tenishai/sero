/**
 * The reveal step-up gate: whether revealing a plaintext value is currently
 * permitted, and how a user obtains that permission.
 *
 * Revealing a secret value is the one action where plaintext reaches the browser.
 * Deployments differ in how much friction that should carry: none, a re-auth, or
 * a full identity-provider step-up. So the gate is an interface, and Ladon ships
 * an "always-allow" default (no extra step). A deployment plugs in a stricter
 * gate (re-auth prompt, OIDC popup) without touching the reveal UI.
 *
 * A gate reports a grant with an optional expiry; the reveal store polls it for a
 * countdown and re-gates when it lapses. The gate governs PERMISSION only; the
 * actual plaintext fetch (api.revealValue) is separate and value-free until then.
 */

export interface RevealGrant {
  active: boolean;
  /** Unix ms when the grant lapses, or null for a grant with no expiry. */
  expiresAt: number | null;
}

export interface RevealGate {
  /** The current grant, without prompting. Called on load to hydrate state. */
  status(): Promise<RevealGrant>;
  /** Ensure an active grant, prompting the user (step-up) if needed. */
  ensureActive(): Promise<RevealGrant>;
  /** End the current grant. */
  revoke(): Promise<void>;
}

/**
 * The default gate: reveal is always permitted, no step-up, no expiry. Neutral
 * baseline; a deployment that wants friction supplies its own gate.
 */
export class AlwaysAllowGate implements RevealGate {
  async status(): Promise<RevealGrant> {
    return { active: true, expiresAt: null };
  }
  async ensureActive(): Promise<RevealGrant> {
    return { active: true, expiresAt: null };
  }
  async revoke(): Promise<void> {
    // Nothing to revoke: this gate always permits reveal.
  }
}
