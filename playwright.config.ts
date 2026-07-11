import { defineConfig } from '@playwright/test';

/**
 * Минимальный конфиг Playwright для логических тестов модуля wakeLock.js.
 * Тесты не запускают FastAPI-сервер — грузят модуль напрямую в about:blank
 * и мокают navigator.wakeLock / window.NoSleep.
 */
export default defineConfig({
  testDir: './tests',
  timeout: 10_000,
  fullyParallel: true,
  use: {
    headless: true,
  },
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
  ],
});
