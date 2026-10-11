import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vite";

// The server serves dist/ at /admin/. `npm run dev` proxies the API to a
// server on the default port.
export default defineConfig({
  base: "/admin/",
  plugins: [svelte()],
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    proxy: { "/v1": "http://127.0.0.1:8765" },
  },
});
