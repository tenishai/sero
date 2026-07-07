import { mount } from "svelte";
import "./app.css";
import { branding } from "./branding";
import { theme } from "$lib/theme.svelte";
import { router } from "$lib/router.svelte";
import App from "./App.svelte";

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
