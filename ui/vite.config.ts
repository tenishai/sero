import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";

// Ladon UI build. Tailwind v4 is configured entirely in CSS (see src/app.css);
// there is no tailwind.config.js. The dev server proxies the backend API so the
// UI runs same-origin in development.
export default defineConfig({
  plugins: [svelte(), tailwindcss()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
