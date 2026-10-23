import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  // Frontend buduje sie do katalogu statycznego pakietu Pythona:
  // jeden port, bez CORS (BRO-76).
  build: { outDir: "../src/cockpit/static", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8765" } },
});
