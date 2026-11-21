import { defineConfig } from '@playwright/test';
// All API requests in this suite are intercepted. Never reuse a running live server.
const port = Number(process.env.LIVEPROBE_UNIFIED_UI_PORT || 4187);
const outDir = process.env.LIVEPROBE_DIST ? ` --outDir ${JSON.stringify(process.env.LIVEPROBE_DIST)}` : '';
export default defineConfig({ testDir: './e2e', testMatch: 'unified.spec.mjs', timeout: 30000, retries: 0, workers: 1, reporter: 'list', outputDir: process.env.LIVEPROBE_E2E_OUTPUT || '/private/tmp/liveprobe-unified-ui-results', use: { baseURL: `http://127.0.0.1:${port}`, headless: true, screenshot: 'only-on-failure' }, projects: [375, 768, 1440].map(width => ({ name: `unified-${width}`, use: { viewport: { width, height: 1000 } } })), webServer: { command: `npm run preview -- --host 127.0.0.1 --port ${port} --strictPort${outDir}`, url: `http://127.0.0.1:${port}`, reuseExistingServer: false } });
