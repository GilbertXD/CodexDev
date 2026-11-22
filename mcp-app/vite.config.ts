import { defineConfig } from "vite";
import { viteSingleFile } from "vite-plugin-singlefile";
import { resolve } from "node:path";

export default defineConfig({
  root: resolve(__dirname, "src/ui"),
  plugins: [viteSingleFile()],
  build: {
    outDir: resolve(__dirname, "dist"),
    emptyOutDir: false,
    cssCodeSplit: false,
    assetsInlineLimit: 100_000_000,
    rollupOptions: {
      input: resolve(__dirname, "src/ui/index.html"),
      output: {
        entryFileNames: "monitor.js",
      },
    },
  },
});
