/*
 * wakeLock.js — держит экран включённым, пока открыта страница игрока.
 *
 * Прогрессивная стратегия:
 *   1) Основной путь — нативный Screen Wake Lock API
 *      (navigator.wakeLock.request('screen')). Работает ТОЛЬКО в secure context
 *      (HTTPS или localhost). Игра обычно идёт по http://<ip> в локальной сети,
 *      где браузер молча отклоняет запрос — тогда включается фолбэк.
 *   2) Фолбэк — библиотека NoSleep.js (window.NoSleep, подключается отдельным
 *      <script src="/static/nosleep.min.js">). Держит экран через зациклённое
 *      скрытое видео; работает и на http. Ручной видео-хак НЕ дублируем.
 *
 * Особенности:
 *   - enable() ДОЛЖЕН вызываться из пользовательского жеста (клик/тап), иначе
 *     autoplay-политики заблокируют и wake lock, и видео.
 *   - переустанавливает лок на 'visibilitychange', когда вкладка снова видима
 *     (ОС отбирает нативный лок, пока вкладка скрыта);
 *   - освобождает лок при disable() и при скрытии/закрытии страницы (pagehide);
 *   - логирует состояние (acquired / released / fallback-triggered / failed)
 *     в консоль и, если передан колбэк analytics, отправляет событие.
 *
 * API:
 *   var mgr = createWakeLockManager({ onStateChange: fn, analytics: fn });
 *   mgr.enable();   // из обработчика клика
 *   mgr.disable();  // явный выход из режима
 *   mgr.getState(); // 'inactive' | 'active-native' | 'active-fallback' | 'failed'
 */
(function (global) {
  'use strict';

  var STATE = {
    INACTIVE: 'inactive',
    NATIVE: 'active-native',
    FALLBACK: 'active-fallback',
    FAILED: 'failed'
  };

  function createWakeLockManager(opts) {
    opts = opts || {};
    var onStateChange = typeof opts.onStateChange === 'function' ? opts.onStateChange : function () {};
    var analytics = typeof opts.analytics === 'function' ? opts.analytics : null;
    var LOG = '[wakeLock]';

    var state = STATE.INACTIVE;
    var enabled = false;        // намерение пользователя: держать экран включённым
    var nativeLock = null;      // WakeLockSentinel
    var noSleep = null;         // экземпляр NoSleep
    var fallbackEnabled = false; // NoSleep уже поднят (для re-acquire без жеста)
    var supportsNative = !!(navigator.wakeLock && typeof navigator.wakeLock.request === 'function');

    function report(evt, extra) {
      try { console.log(LOG, evt, extra || ''); } catch (e) {}
      if (analytics) {
        try { analytics('wake_lock_' + evt, extra || {}); } catch (e) {}
      }
    }

    function setState(s) {
      if (s === state) return;
      state = s;
      try { onStateChange(s); } catch (e) {}
    }

    async function acquireNative() {
      if (!supportsNative) return false;
      try {
        if (nativeLock) {
          try { await nativeLock.release(); } catch (e) {}
          nativeLock = null;
        }
        nativeLock = await navigator.wakeLock.request('screen');
        nativeLock.addEventListener('release', function () { nativeLock = null; });
        return true;
      } catch (e) {
        // Отклонён — обычно insecure context (http в локальной сети).
        return false;
      }
    }

    function acquireFallback(isGesture) {
      if (typeof global.NoSleep !== 'function') {
        report('failed', { reason: 'nosleep_missing' });
        return false;
      }
      try {
        if (!noSleep) noSleep = new global.NoSleep();
        // NoSleep-видео требует пользовательский жест. При переустановке из
        // visibilitychange (не жест) повторный enable() может быть отклонён
        // autoplay-политикой (iOS). Если фолбэк уже поднят — не дёргаем заново.
        if (!isGesture && fallbackEnabled) return true;
        noSleep.enable();
        fallbackEnabled = true;
        return true;
      } catch (e) {
        report('failed', { reason: 'nosleep_enable_error' });
        return false;
      }
    }

    async function acquire(isGesture) {
      if (await acquireNative()) {
        setState(STATE.NATIVE);
        report('acquired', { mechanism: 'native' });
        return true;
      }
      if (acquireFallback(isGesture)) {
        setState(STATE.FALLBACK);
        report('fallback-triggered', {
          reason: supportsNative ? 'native_denied' : 'native_unsupported'
        });
        return true;
      }
      setState(STATE.FAILED);
      report('failed', { reason: 'no_mechanism' });
      return false;
    }

    async function releaseAll() {
      if (nativeLock) {
        try { await nativeLock.release(); } catch (e) {}
        nativeLock = null;
      }
      if (noSleep) {
        try { noSleep.disable(); } catch (e) {}
      }
      fallbackEnabled = false;
    }

    function onVisibilityChange() {
      if (!enabled) return;
      if (document.visibilityState === 'visible') {
        // Пока вкладка была скрыта, ОС могла отобрать нативный лок — поднимаем
        // заново. Это не пользовательский жест (isGesture=false).
        acquire(false);
      }
    }

    function onPageHide() {
      if (!enabled) return; // не было активного лока — нечего освобождать, не шумим
      // Best-effort освобождение при скрытии/закрытии страницы (async release
      // может не успеть, поэтому вызываем синхронно и глушим ошибки).
      if (nativeLock) {
        try { nativeLock.release(); } catch (e) {}
        nativeLock = null;
      }
      if (noSleep) {
        try { noSleep.disable(); } catch (e) {}
      }
      fallbackEnabled = false;
      report('released', { reason: 'pagehide' });
    }

    // Публичный вход. Вызывать ТОЛЬКО из обработчика пользовательского жеста.
    function enable() {
      enabled = true;
      return acquire(true);
    }

    async function disable() {
      enabled = false;
      await releaseAll();
      setState(STATE.INACTIVE);
      report('released', { reason: 'user_disable' });
    }

    document.addEventListener('visibilitychange', onVisibilityChange);
    window.addEventListener('pagehide', onPageHide);

    return {
      enable: enable,
      disable: disable,
      getState: function () { return state; },
      isEnabled: function () { return enabled; },
      supportsNative: supportsNative,
      STATE: STATE,
      // экспортировано для тестов
      _acquire: acquire,
      _onVisibilityChange: onVisibilityChange
    };
  }

  createWakeLockManager.STATE = STATE;
  global.createWakeLockManager = createWakeLockManager;

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = createWakeLockManager;
    module.exports.STATE = STATE;
  }
})(typeof window !== 'undefined' ? window : this);
