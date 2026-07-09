import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // Honor the port assigned by the harness/env; fall back to Vite's default.
    port: process.env.PORT ? Number(process.env.PORT) : undefined,
    proxy: { "/api": "http://localhost:8000" },
  },
});
