import react from "@vitejs/plugin-react";
import { resolve } from "path";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react({ jsxRuntime: "classic" })],
  build: {
    lib: {
      entry: resolve(__dirname, "src/index.tsx"),
      formats: ["es"],
      fileName: () => "index.js",
    },
    outDir: resolve(__dirname, "dist"),
    emptyOutDir: true,
    // Inline every asset (avatars, office background) as base64 so the plugin
    // ships as a single self-contained index.js. This avoids relative-asset
    // URL resolution issues when the bundle is loaded dynamically by the host.
    assetsInlineLimit: 100 * 1024 * 1024,
    rollupOptions: {
      external: ["react", "react-dom"],
    },
  },
});
