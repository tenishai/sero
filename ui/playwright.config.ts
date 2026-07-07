import { defineConfig, devices } from "@playwright/test";

// A minimal end-to-end smoke check: run the dev server and load the app in a
// real browser to catch render-time failures (a blank page, a missing provider,
// a component throwing on mount) that svelte-check and vite build do not - the
// class of bug that has slipped through twice.
//
// It runs against the DEV server (not the production preview) because the ?mock
// data source is gated to dev mode, so the app renders fully populated with no
// backend.
const PORT = 5199;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: "list",
  use: {
    baseURL: `http://localhost:${PORT}`,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    // The dev server, so ?mock (dev-gated) renders the app fully populated.
    command: `npm run dev -- --port ${PORT} --strictPort`,
    port: PORT,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
