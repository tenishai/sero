/**
 * Client-side secrets hygiene, mirroring the backend's rules so the UI catches
 * mistakes before a request. The backend enforces these again; the client check
 * is for immediate feedback, not the source of truth.
 */

// Values that almost always indicate a mistake rather than a real secret.
// Compared case-insensitively after trimming. Kept in sync with the backend.
const PLACEHOLDERS = new Set([
  "",
  "changeme",
  "change-me",
  "change_me",
  "password",
  "passwd",
  "secret",
  "todo",
  "tbd",
  "replace_me",
  "replaceme",
  "replace-me",
  "xxx",
  "example",
  "placeholder",
]);

export function isPlaceholder(value: string): boolean {
  return PLACEHOLDERS.has(value.trim().toLowerCase());
}

/** The keys whose values look like placeholders. Empty when all values are real. */
export function placeholderKeys(values: Record<string, string>): string[] {
  return Object.entries(values)
    .filter(([, v]) => isPlaceholder(v))
    .map(([k]) => k);
}
