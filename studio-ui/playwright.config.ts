// CineCrew Studio UI — Playwright config. Starts the real Studio server (API + worker + built UI)
// against a fresh temporary STUDIO_HOME in OFFLINE FIXTURE mode; nothing paid is ever called.
import { defineConfig } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const port = Number(process.env.STUDIO_TEST_PORT ?? 8777);
const home = fs.mkdtempSync(path.join(os.tmpdir(), "cinecrew-ui-test-"));
const python = process.env.STUDIO_PYTHON ?? path.join(repo, ".venv", "bin", "python");
const chromium = process.env.PW_CHROMIUM_PATH ?? "";

export default defineConfig({
  testDir: "./tests",
  timeout: 15 * 60 * 1000,
  expect: { timeout: 30_000 },
  workers: 1,
  reporter: [["list"]],
  outputDir: "test-results",
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    locale: "vi-VN",
    viewport: { width: 1360, height: 900 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    launchOptions: fs.existsSync(chromium) ? { executablePath: chromium } : {},
  },
  webServer: {
    command: `"${python}" -m src.studio serve --port ${port}`,
    cwd: repo,
    url: `http://127.0.0.1:${port}/api/health`,
    reuseExistingServer: false,
    timeout: 60_000,
    env: { STUDIO_HOME: home },
    stdout: "pipe",
    stderr: "pipe",
  },
});
