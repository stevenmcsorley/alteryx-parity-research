import { defineConfig } from '@playwright/test';

// End-to-end UI tests run the real engine (loopback HTTP transport) and the built UI.
const ENGINE_PORT = 8799;
const python = process.env.FLOWENGINE_PYTHON || 'python';

export default defineConfig({
  testDir: 'e2e',
  timeout: 60_000,
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:4173',
    viewport: { width: 1500, height: 920 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: [
    {
      command: `${python} -m flowengine serve --http ${ENGINE_PORT}`,
      cwd: '..',
      url: `http://127.0.0.1:${ENGINE_PORT}/health`,
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: 'npx vite build && npx vite preview --port 4173 --strictPort --host 127.0.0.1',
      url: 'http://127.0.0.1:4173',
      reuseExistingServer: false,
      timeout: 120_000,
      env: { VITE_ENGINE_URL: `http://127.0.0.1:${ENGINE_PORT}` },
    },
  ],
});
