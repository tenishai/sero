/**
 * Access derivations from the signed-in user.
 *
 * Admin-ness is the backend's answer (`is_admin` on /api/me), not something the
 * client infers. The backend takes it from the deployment config's `access
 * .admins`, where being an admin grants no namespace, so guessing it from
 * namespace membership would name the wrong people in both directions.
 */

import type { Me } from "./api.ts";

export function isAdmin(me: Me | undefined): boolean {
  return !!me?.is_admin;
}
