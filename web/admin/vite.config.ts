import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

function normalizedBase(value: string | undefined) {
  const raw = (value || "/").trim();
  const withLeadingSlash = raw.startsWith("/") ? raw : "/" + raw;
  return withLeadingSlash.endsWith("/") ? withLeadingSlash : withLeadingSlash + "/";
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  return {
    base: normalizedBase(env.VITE_PUBLIC_PATH_PREFIX),
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
  };
});
