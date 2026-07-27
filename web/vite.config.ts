import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: proxy the API to the local FastAPI server so the Vite dev server (5173)
// can talk to it. Prod: the app is built to web/dist and served by FastAPI
// same-origin, so no proxy is used.
export default defineConfig({
  plugins: [react()],
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
