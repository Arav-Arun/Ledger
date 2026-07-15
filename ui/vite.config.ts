import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // Honor the port assigned by the harness/env; fall back to Vite's default.
    port: process.env.PORT ? Number(process.env.PORT) : undefined,
    // API dev server port; defaults to 8000, override with LEDGER_API_PORT if 8000 is taken.
    proxy: { "/api": `http://localhost:${process.env.LEDGER_API_PORT ?? 8000}` },
  },
});
