import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
// Game server target. Override with GAME_URL=http://host:port bun run dev.
const TARGET = process.env.GAME_URL ?? "http://192.168.50.167:8000";
const WS_TARGET = TARGET.replace(/^http/, "ws");
export default defineConfig({
    plugins: [react()],
    server: {
        port: 5173,
        strictPort: true,
        proxy: {
            "/api": { target: TARGET, changeOrigin: true },
            "/ws": { target: WS_TARGET, ws: true, changeOrigin: true },
        },
    },
});
