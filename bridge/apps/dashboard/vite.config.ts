import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: process.env.VITE_BASE_PATH || "/",
  plugins: [react()],
  test: {
    environment: "jsdom",
    exclude: ["**/*.e2e.ts", "node_modules/**", "dist/**"],
    include: ["src/**/*.test.tsx"],
    setupFiles: "./src/test-setup.ts",
    globals: true
  }
});
