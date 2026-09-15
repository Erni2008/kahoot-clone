import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig({
    base: "/static/studio/",
    plugins: [react()],
    build: {
        outDir: "../static/studio",
        emptyOutDir: true,
        sourcemap: true,
        target: "es2022",
    },
    server: {
        port: 5173,
        proxy: { "/api": "http://127.0.0.1:8000" },
    },
});
