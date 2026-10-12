import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";

// Sero UI build. Tailwind v4 is configured entirely in CSS (see src/app.css);
// there is no tailwind.config.js. The dev server proxies the backend API so the
// UI runs same-origin in development.
//
// The $lib/$components/$views aliases must be declared here for Vite to resolve
// them at dev and build time; the matching tsconfig `paths` only inform the type
// checker. Keep the two in sync.
const alias = {
  $lib: fileURLToPath(new URL("./src/lib", import.meta.url)),
  $components: fileURLToPath(new URL("./src/components", import.meta.url)),
  $views: fileURLToPath(new URL("./src/views", import.meta.url)),
};

export default defineConfig({
  resolve: { alias },
  plugins: [svelte(), tailwindcss()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
