import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

import { defineConfig, devices } from "@playwright/test";
import type { PlaywrightTestConfig } from "@playwright/test";

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
  trace: "retain-on-failure"
};

if (executablePath) {
  dashboardUse.launchOptions = {
    executablePath
  };
}

export default defineConfig({
  testDir: "./e2e",
  timeout: 30000,
  expect: {
    timeout: 10000
  },
  use: dashboardUse,
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 4173",
    reuseExistingServer: true,
    timeout: 30000,
    url: "http://127.0.0.1:4173"
  },
  projects: [
    {
      name: "chromium",
      use: devices["Desktop Chrome"]
    }
  ]
});
