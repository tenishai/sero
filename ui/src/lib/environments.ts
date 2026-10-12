/**
 * Which environments this deployment actually serves.
 *
 * One Sero talks to one OpenBao, which holds one environment's secrets, so the
 * backend refuses a request naming any other and reports the one it serves on
 * /api/me. Offering the branding list regardless would show choices that answer
 * 400 on click.
 *
 * The branding list stays the fallback, for an instance that has not declared
 * an environment: it pins nothing, so any of them is as good a guess as another.
 */

import { branding } from "../branding.ts";
import type { Me } from "./api.ts";

export function environmentsFor(me: Me | undefined): string[] {
  return me?.environment ? [me.environment] : branding.environments;
}

/** The environment to select, given what the deployment has said so far. */
export function defaultEnvironment(me: Me | undefined): string {
  return environmentsFor(me)[0];
}
