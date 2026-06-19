import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig, devices } from "@playwright/test";
import type { PlaywrightTestConfig } from "@playwright/test";

const e2eRoot = dirname(fileURLToPath(import.meta.url));
const dashboardRoot = join(e2eRoot, "..");
const envExecutablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
const cachedExecutablePath = join(
  homedir(),
  "Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
);

let executablePath = "";
if (envExecutablePath && existsSync(envExecutablePath)) {
  executablePath = envExecutablePath;
}

if (!executablePath && existsSync(cachedExecutablePath)) {
  executablePath = cachedExecutablePath;
}

const dashboardUse: PlaywrightTestConfig["use"] = {
  baseURL: "http://127.0.0.1:4173",
  screenshot: "only-on-failure",
  trace: "retain-on-failure"
};

if (executablePath) {
  dashboardUse.launchOptions = {
    executablePath
  };
}

export default defineConfig({
  expect: {
    timeout: 10000
  },
  outputDir: join(e2eRoot, "test-results"),
  projects: [
    {
      name: "chromium",
      use: devices["Desktop Chrome"]
    }
  ],
  reporter: [
    ["list"],
    ["html", { open: "never", outputFolder: join(e2eRoot, "playwright-report") }]
  ],
  testDir: e2eRoot,
  timeout: 30000,
  use: dashboardUse,
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 4173",
    cwd: dashboardRoot,
    env: { ...process.env, VITE_AUTH_DISABLED: "true" },
    reuseExistingServer: true,
    timeout: 30000,
    url: "http://127.0.0.1:4173"
  }
});
