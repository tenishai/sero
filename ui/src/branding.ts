/**
 * Branding and deployment configuration: the ONE place a new client edits.
 *
 * Ladon ships unbranded. To make the UI a client's own, edit this file (product
 * name, wordmark, admin-namespace concept, environments, doc links, storage
 * prefix) and the palette/fonts in `app.css`. Components and views must never
 * hardcode any of these; they import from here. That is the whole "drop it in,
 * rebrand, go" contract, and it is enforced by keeping identity out of the rest
 * of the tree.
 *
 * Nothing here is a secret. It is public configuration compiled into the bundle.
 */

export interface Branding {
  /** Product name shown in the sidebar wordmark and the document title. */
  productName: string;
  /** Short tagline under the wordmark. Empty string hides it. */
  tagline: string;
  /**
   * The privileged namespace whose members see admin surfaces. Generic here so
   * no client's team name is baked into the code.
   */
  adminNamespace: string;
  /**
   * The environments a deployment manages, in display order. The source hard-
   * coded dev/qa/prod; here it is configuration so a client can differ.
   */
  environments: string[];
  /** localStorage key prefix, so multiple deployments on one origin do not collide. */
  storagePrefix: string;
  /** External documentation links shown in the getting-started surface. */
  docs: {
    gettingStarted?: string;
    requestAccess?: string;
  };
}

/**
 * The default, unbranded configuration. A real deployment overrides these
 * values (and the palette in app.css); the defaults are deliberately generic
 * and carry no company, cloud, or product-specific identity.
 */
export const branding: Branding = {
  productName: "Ladon",
  tagline: "Secrets platform",
  adminNamespace: "platform-admin",
  environments: ["dev", "prod"],
  storagePrefix: "ladon",
  docs: {},
};
