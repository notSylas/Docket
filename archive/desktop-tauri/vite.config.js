import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import process from "node:process";
const host = process.env.TAURI_DEV_HOST;

// https://vite.dev/config/
export default defineConfig(() => ({
  plugins: [react(), tailwindcss()],

  // Vite options tailored for Tauri development and only applied in `tauri dev` or `tauri build`
  //
  // 1. prevent Vite from obscuring rust errors
  clearScreen: false,
  // 2. tauri expects a fixed port, fail if that port is not available
  server: {
    port: 1420,
    strictPort: true,
    host: host || "127.0.0.1",
    hmr: host
      ? {
          protocol: "ws",
          host,
          port: 1421,
        }
      : undefined,
    watch: {
      // 3. tell Vite to ignore watching `src-tauri` and the sidecar's
      // Python venvs/build output -- `.build-venv` in particular bundles
      // torch (tens of thousands of files), which blows past the OS's
      // inotify watch limit (ENOSPC) if Vite tries to watch it.
      //
      // A glob-string `ignored` (e.g. "**/sidecar/.build-venv/**") does
      // NOT reliably prune directory recursion in Vite's bundled watcher
      // (confirmed empirically during the IPC-wiring checkpoint: the dev
      // server still crashed with ENOSPC while walking `.build-venv` even
      // with that glob configured) -- a predicate function is what
      // actually stops it from descending into these directories at all.
      ignored: (path) =>
        /[/\\]src-tauri[/\\]/.test(path) ||
        /[/\\]sidecar[/\\]\.build-venv([/\\]|$)/.test(path) ||
        /[/\\]sidecar[/\\]build([/\\]|$)/.test(path) ||
        /[/\\]sidecar[/\\]dist([/\\]|$)/.test(path),
    },
  },
}));
