import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

// Minimal Svelte config: enable TypeScript preprocessing. No SvelteKit; this is
// a Vite-built SPA.
export default {
  preprocess: vitePreprocess(),
};
