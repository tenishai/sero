/**
 * Light/dark theme store (Svelte 5 runes).
 *
 * Persists the choice to localStorage under the branding storage prefix, falls
 * back to the OS preference, and toggles `theme-light` on <html>. The palette
 * itself lives in app.css; this only flips which set of CSS variables is active.
 */

import { branding } from "../branding";

export type Theme = "dark" | "light";

const STORAGE_KEY = `${branding.storagePrefix}:theme`;

function initialTheme(): Theme {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored === "dark" || stored === "light") return stored;
  const prefersLight =
    typeof matchMedia === "function" && matchMedia("(prefers-color-scheme: light)").matches;
  return prefersLight ? "light" : "dark";
}

function apply(theme: Theme): void {
  const root = document.documentElement;
  // Briefly disable transitions so the swap does not animate every element.
  root.classList.add("theme-switching");
  root.classList.toggle("theme-light", theme === "light");
  // Force a reflow, then re-enable transitions on the next frame.
  void root.offsetWidth;
  requestAnimationFrame(() => root.classList.remove("theme-switching"));
}

class ThemeStore {
  current = $state<Theme>("dark");

  init(): void {
    this.current = initialTheme();
    apply(this.current);
  }

  set(theme: Theme): void {
    this.current = theme;
    localStorage.setItem(STORAGE_KEY, theme);
    apply(theme);
  }

  toggle(): void {
    this.set(this.current === "dark" ? "light" : "dark");
  }
}

export const theme = new ThemeStore();
