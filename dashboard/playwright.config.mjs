import { defineConfig } from '@playwright/test';
const mock = process.env.LIVEPROBE_MOCK === '1';
const mockPort = Number(process.env.LIVEPROBE_MOCK_PORT || 8015);
const uiPort = Number(process.env.LIVEPROBE_TEST_UI_PORT || 4175);
export default defineConfig({
  testDir: './e2e', timeout: 30000, retries: 0, workers: 1, reporter: 'list', outputDir: './e2e/test-results',
  use: { baseURL: mock ? `http://127.0.0.1:${uiPort}` : (process.env.LIVEPROBE_URL ?? 'http://liveprobe.localhost'), headless: true, screenshot: 'only-on-failure' },
  projects: [375, 768, 1440].map(width => ({ name: `width-${width}`, use: { viewport: { width, height: width === 375 ? 844 : 1000 } } })),
  webServer: mock ? [
    { command: 'node e2e/mock-api.mjs', url: `http://127.0.0.1:${mockPort}/api/overview`, reuseExistingServer: false },
    { command: `LIVEPROBE_MOCK_API=http://127.0.0.1:${mockPort} npm run preview -- --host 127.0.0.1 --port ${uiPort} --strictPort`, url: `http://127.0.0.1:${uiPort}`, reuseExistingServer: false },
  ] : undefined,
});
