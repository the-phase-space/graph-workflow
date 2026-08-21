import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "dist",
    target: "es2022",
  },
  clearScreen: false,
  server: {
    port: 5183,
    strictPort: true,
  },
});
