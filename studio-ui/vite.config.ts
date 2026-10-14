import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backend = process.env.STUDIO_API ?? "http://127.0.0.1:8765";

export default defineConfig({
  base: "/",
  plugins: [react()],
  server: {
    proxy: {
      "/api": backend,
      "/media": backend,
    },
  },
});
