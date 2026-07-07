import { mount } from "svelte";
import "./app.css";
import { branding } from "./branding";
import { theme } from "$lib/theme.svelte";
import { router } from "$lib/router.svelte";
import { ApiClient } from "$lib/api";
import { setApiClient } from "$lib/queries";
import App from "./App.svelte";

// Dev-only mock: with `?mock` (or VITE_MOCK) in a dev build, point the API client
// at canned data so the UI renders fully populated with no backend. Never active
// in a production build (guarded by import.meta.env.DEV).
if (import.meta.env.DEV) {
  const wantMock =
    new URLSearchParams(location.search).has("mock") || import.meta.env.VITE_MOCK === "1";
  if (wantMock) {
    const { devMockTransport } = await import("$lib/devMock");
    setApiClient(new ApiClient(devMockTransport));
  }
}

// Reflect the configured product name into the document title, so the title is
// driven by branding.ts too (index.html carries a static fallback for first
// paint).
document.title = branding.productName;

theme.init();
router.init();

const target = document.getElementById("app");
if (!target) throw new Error("mount target #app not found");

const app = mount(App, { target });

export default app;
