import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// The dev server proxies /api to the Python API rather than making the browser
// talk to a second origin. That keeps development on one origin, so a cookie
// or a same-origin assumption cannot work locally and break in production.
const proxy = {
  "/api": {
    target: process.env.CLIPFORGE_API_URL ?? "http://127.0.0.1:8000",
    changeOrigin: true,
  },
};

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  server: { port: 5173, proxy },
  // `preview` serves the production build. It gets the same proxy so what is
  // verified before a deploy is the built bundle against the real API, not a
  // dev server with different plumbing.
  preview: { port: 4173, proxy },
  build: { outDir: "dist", sourcemap: true },
});
