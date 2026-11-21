import { defineConfig } from '@playwright/test';
const mock = process.env.LIVEPROBE_MOCK === '1';
// Ports and the preview bundle are overridable so a local run never reuses (or collides with) another server.
const uiPort = Number(process.env.LIVEPROBE_MOCK_UI_PORT || 4175), apiPort = Number(process.env.LIVEPROBE_MOCK_API_PORT || 8015);
const outDir = process.env.LIVEPROBE_DIST ? ` --outDir ${JSON.stringify(process.env.LIVEPROBE_DIST)}` : '';
export default defineConfig({
  testDir: './e2e', timeout: 30000, retries: 0, workers: 1, reporter: 'list', outputDir: process.env.LIVEPROBE_E2E_OUTPUT || './e2e/test-results',
  use: { baseURL: mock ? `http://127.0.0.1:${uiPort}` : (process.env.LIVEPROBE_URL ?? 'http://liveprobe.localhost'), headless: true, screenshot: 'only-on-failure' },
  projects: [375, 768, 1440].map(width => ({ name: `width-${width}`, use: { viewport: { width, height: width === 375 ? 844 : 1000 } } })),
  webServer: mock ? [
    { command: `LIVEPROBE_MOCK_PORT=${apiPort} node e2e/mock-api.mjs`, url: `http://127.0.0.1:${apiPort}/api/overview`, reuseExistingServer: false },
    { command: `LIVEPROBE_MOCK_API=http://127.0.0.1:${apiPort} npm run preview -- --host 127.0.0.1 --port ${uiPort} --strictPort${outDir}`, url: `http://127.0.0.1:${uiPort}`, reuseExistingServer: false },
  ] : undefined,
});
