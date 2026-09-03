import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: proxy the API to the local FastAPI server so the Vite dev server (5173)
// can talk to it. Prod: the app is built to web/dist and served by FastAPI
// same-origin, so no proxy is used.
export default defineConfig({
  plugins: [react()],
  // Stamp the bundle with its build time. The app compares this against the
  // server's start time to detect that it is newer than the process serving it
  // — the failure mode where a long-running server keeps handing out a rebuilt
  // UI whose API endpoints it does not have.
  define: { __BUILD_TIME__: JSON.stringify(new Date().toISOString()) },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    chunkSizeWarningLimit: 1200,
  },
});
