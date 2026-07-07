/**
 * Access derivations from the signed-in user.
 *
 * Admin-ness is not a backend flag; it is derived from whether the user can
 * access the configured admin namespace (branding.adminNamespace). Keeping it
 * config-driven and local keeps the concept brand-neutral: no client's team name
 * is baked into the code, and a deployment sets who is admin by namespace
 * membership.
 */

import { branding } from "../branding.ts";
import type { Me } from "./api.ts";

export function isAdmin(me: Me | undefined): boolean {
  return !!me && me.namespaces.includes(branding.adminNamespace);
}
