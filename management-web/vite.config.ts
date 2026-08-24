import vinext from "vinext";
import { defineConfig } from "vite";

// macOS Seatbelt blocks FSEvents, so Codex previews need polling for HMR.
const isCodexSeatbeltSandbox = process.env.CODEX_SANDBOX === "seatbelt";
const managementApiTarget =
  process.env.MANAGEMENT_API_INTERNAL_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  server: {
    host: "127.0.0.1",
    proxy: {
      "/api": { target: managementApiTarget, changeOrigin: true },
      "/auth": { target: managementApiTarget, changeOrigin: true },
    },
    ...(isCodexSeatbeltSandbox
      ? { watch: { useFsEvents: false, usePolling: true } }
      : {}),
  },
  plugins: [vinext()],
});
