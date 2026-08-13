import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("@tiptap")) return "editor";
          if (id.includes("@tanstack")) return "query";
          if (id.includes("react")) return "react";
          return "vendor";
        }
      }
    }
  }
});
