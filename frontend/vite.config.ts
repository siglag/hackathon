import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// All assets are bundled locally; the production build is served by the API.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8600", changeOrigin: false } },
  },
  build: { outDir: "dist", sourcemap: false, assetsInlineLimit: 0 },
});
