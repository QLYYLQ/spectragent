import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: fileURLToPath(
      new URL("../../spectragent/viewer/static", import.meta.url),
    ),
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    proxy: { "/api": "http://127.0.0.1:8765" },
  },
});
