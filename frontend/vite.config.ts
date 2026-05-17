import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../backend/src/static",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/ws": {
        target: "http://127.0.0.1:8001",
        ws: true,
      },
      "/api": {
        target: "http://127.0.0.1:8001",
      },
    },
  },
});
