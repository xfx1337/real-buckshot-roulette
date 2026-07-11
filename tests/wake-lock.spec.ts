import { test, expect, Page } from '@playwright/test';
import path from 'path';

/**
 * ВАЖНО: эти тесты проверяют ТОЛЬКО КОРРЕКТНОСТЬ ЛОГИКИ модуля wakeLock.js —
 * что он вызывает правильные API в правильном порядке и реагирует на события.
 *
 * Они НЕ проверяют реальную блокировку/разблокировку экрана на устройстве:
 * держит ли ОС дисплей включённым, как ведёт себя энергосбережение, работает ли
 * NoSleep-видео на конкретном iOS/Android — всё это проверяется ТОЛЬКО ручным QA
 * на реальных устройствах (см. WAKE_LOCK.md). Здесь navigator.wakeLock и
 * window.NoSleep замоканы, реального экрана нет.
 */

const WAKE_LOCK_JS = path.resolve(__dirname, '../app/static/wakeLock.js');

/**
 * Готовит страницу: мокает navigator.wakeLock (или убирает его) и window.NoSleep,
 * затем грузит модуль и создаёт менеджер с кнопкой-триггером #btn.
 */
async function setup(page: Page, opts: { native: boolean }) {
  await page.goto('about:blank');

  // Моки ставим ДО загрузки модуля: supportsNative вычисляется в момент
  // createWakeLockManager() по navigator.wakeLock.
  await page.evaluate((hasNative) => {
    (window as any).__calls = { request: 0, release: 0 };
    Object.defineProperty(navigator, 'wakeLock', {
      configurable: true,
      value: hasNative
        ? {
            request: async () => {
              (window as any).__calls.request++;
              return {
                release: async () => {
                  (window as any).__calls.release++;
                },
                addEventListener: () => {},
              };
            },
          }
        : undefined,
    });

    // Спай на NoSleep — фолбэк наблюдаем без реального устройства.
    (window as any).__nosleep = { enabled: 0, disabled: 0 };
    (window as any).NoSleep = function () {
      return {
        enable: () => {
          (window as any).__nosleep.enabled++;
        },
        disable: () => {
          (window as any).__nosleep.disabled++;
        },
      };
    };
  }, opts.native);

  await page.addScriptTag({ path: WAKE_LOCK_JS });

  // Кнопка «Не выключать экран» — enable() строго по клику (жест пользователя).
  await page.evaluate(() => {
    (window as any).__states = [];
    const mgr = (window as any).createWakeLockManager({
      onStateChange: (s: string) => (window as any).__states.push(s),
    });
    (window as any).mgr = mgr;
    const btn = document.createElement('button');
    btn.id = 'btn';
    btn.textContent = 'Не выключать экран';
    btn.addEventListener('click', () => mgr.enable());
    document.body.appendChild(btn);
  });
}

test('нативный wake lock запрашивается по клику на кнопку', async ({ page }) => {
  await setup(page, { native: true });

  // До клика — ничего не запрошено (нет авто-старта).
  expect(await page.evaluate(() => (window as any).__calls.request)).toBe(0);

  await page.click('#btn');

  expect(await page.evaluate(() => (window as any).__calls.request)).toBe(1);
  expect(await page.evaluate(() => (window as any).mgr.getState())).toBe('active-native');
  // Фолбэк НЕ трогаем, если нативный сработал.
  expect(await page.evaluate(() => (window as any).__nosleep.enabled)).toBe(0);
});

test('лок переустанавливается при visibilitychange (вкладка снова видима)', async ({ page }) => {
  await setup(page, { native: true });
  await page.click('#btn');
  expect(await page.evaluate(() => (window as any).__calls.request)).toBe(1);

  // Симулируем скрытие вкладки, затем возврат.
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => 'hidden',
    });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  // Пока скрыта — повторный запрос не делаем.
  expect(await page.evaluate(() => (window as any).__calls.request)).toBe(1);

  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => 'visible',
    });
    document.dispatchEvent(new Event('visibilitychange'));
  });

  // Вернулись — модуль переустанавливает лок (release старого + новый request).
  await expect
    .poll(() => page.evaluate(() => (window as any).__calls.request))
    .toBe(2);
});

test('при отсутствии navigator.wakeLock срабатывает фолбэк NoSleep.js', async ({ page }) => {
  await setup(page, { native: false });

  expect(await page.evaluate(() => (window as any).mgr.supportsNative)).toBe(false);

  await page.click('#btn');

  // Нативный не звали, NoSleep.enable() вызван → состояние active-fallback.
  expect(await page.evaluate(() => (window as any).__calls.request)).toBe(0);
  await expect
    .poll(() => page.evaluate(() => (window as any).__nosleep.enabled))
    .toBe(1);
  expect(await page.evaluate(() => (window as any).mgr.getState())).toBe('active-fallback');
});

test('disable() освобождает лок и возвращает состояние inactive', async ({ page }) => {
  await setup(page, { native: true });
  await page.click('#btn');
  expect(await page.evaluate(() => (window as any).mgr.getState())).toBe('active-native');

  await page.evaluate(() => (window as any).mgr.disable());

  await expect
    .poll(() => page.evaluate(() => (window as any).__calls.release))
    .toBe(1);
  expect(await page.evaluate(() => (window as any).mgr.getState())).toBe('inactive');
});
