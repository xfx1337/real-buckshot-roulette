/*
 * Выбор устройства вывода звука — Buckshot Roulette IRL.
 *
 * Задача: звук видеоконтента должен идти на телевизор, а звуки игры — на
 * колонку. Браузер умеет это через `HTMLMediaElement.setSinkId(deviceId)`:
 * каждый media-элемент можно направить на свой физический выход.
 *
 * Каналы:
 *   'game'  — звуки игрового процесса (SoundEngine на панели дилера);
 *   'video' — видеоконтент и CCTV на TV-экране.
 *
 * Выбор оператора хранится на сервере (`/api/audio/outputs`), чтобы он пережил
 * перезагрузку страницы и был виден всем экранам. deviceId браузера стабилен
 * для origin, пока пользователь не сбросит разрешения сайта.
 *
 * Ограничения:
 *   - `setSinkId` есть в Chrome/Edge/Opera; в Safari и Firefox (по умолчанию)
 *     его нет — там выбор недоступен, звук идёт на системное устройство.
 *     `isSupported()` говорит об этом панели оператора.
 *   - `navigator.mediaDevices` существует ТОЛЬКО в secure context: https или
 *     localhost/127.0.0.1. Панель, открытая по LAN-адресу (http://192.168.x.x),
 *     этого API не получает вовсе — список устройств будет пуст. Поэтому
 *     `unavailableReason()` отличает «нет secure context» от «нет поддержки
 *     браузера», чтобы оператор видел причину и знал, что делать.
 *   - Осмысленные названия устройств (labels) браузер отдаёт только после
 *     выданного разрешения на медиа. Поэтому `listDevices()` при пустых
 *     подписях один раз запрашивает микрофон и сразу же его отключает —
 *     запись не ведётся, нужен только доступ к списку устройств.
 *   - Элементы, созданные ДО загрузки выбора, догоняются: `register()`
 *     запоминает элемент и применяет sink повторно при каждой смене.
 */
(function () {
  var DEBUG = true;
  function log() { if (DEBUG && window.console) console.log.apply(console, ['[audio-out]'].concat([].slice.call(arguments))); }

  var CHANNELS = ['game', 'video'];

  // Текущий deviceId по каналам ('' = системное устройство по умолчанию).
  var sinks = { game: '', video: '' };
  // Живые media-элементы по каналам — WeakRef недоступен везде, поэтому
  // держим массив и чистим отсоединённые элементы при каждом применении.
  var elements = { game: [], video: [] };
  var loaded = false;
  var loadPromise = null;

  function isSupported() {
    return typeof HTMLMediaElement !== 'undefined'
      && typeof HTMLMediaElement.prototype.setSinkId === 'function';
  }

  function canEnumerate() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.enumerateDevices);
  }

  /**
   * Почему выбор устройства недоступен, либо null если всё в порядке.
   * Возвращает {code, message} — панель оператора показывает message как есть.
   */
  function unavailableReason() {
    // Перечисление устройств живёт только в secure context. По LAN-адресу
    // (http://192.168.x.x) браузер не даёт даже сам объект mediaDevices.
    if (!canEnumerate()) {
      if (!window.isSecureContext) {
        return {
          code: 'insecure',
          message: 'Список устройств доступен только по защищённому адресу. '
            + 'Откройте панель по http://localhost:' + (location.port || '80')
            + ' (или по https) — по адресу ' + location.hostname
            + ' браузер скрывает устройства вывода.',
        };
      }
      return {
        code: 'no-enumerate',
        message: 'Браузер не отдаёт список аудиоустройств (enumerateDevices недоступен).',
      };
    }
    if (!isSupported()) {
      return {
        code: 'no-setsinkid',
        message: 'Этот браузер не умеет выбирать устройство вывода (setSinkId). '
          + 'Работает в Chrome / Edge. Сейчас звук идёт на системное устройство по умолчанию.',
      };
    }
    return null;
  }

  function applyTo(el, channel) {
    if (!el || !isSupported()) return Promise.resolve(false);
    var id = sinks[channel] || '';
    // Пустой sinkId в Chrome означает «устройство по умолчанию» — так и шлём.
    if (el.sinkId === id) return Promise.resolve(true);
    return el.setSinkId(id).then(function () {
      log('sink applied', channel, id || '(default)');
      return true;
    }).catch(function (err) {
      // Частый случай: сохранённое устройство отключили (колонку выключили).
      log('setSinkId FAILED', channel, id, err && err.name);
      return false;
    });
  }

  function applyAll(channel) {
    var list = elements[channel] || [];
    // Чистим элементы, выброшенные из DOM и не проигрывающие ничего.
    elements[channel] = list.filter(function (el) {
      return el && (el.isConnected || !el.paused);
    });
    elements[channel].forEach(function (el) { applyTo(el, channel); });
  }

  /**
   * Привязать media-элемент к каналу: sink применится сейчас и при сменах.
   * Возвращает Promise применения — звук нужно запускать ПОСЛЕ него, иначе
   * play() успеет стартовать на старом устройстве (setSinkId асинхронный).
   * Если выбор ещё не загружен с сервера, ждём и его — иначе первый же звук
   * уйдёт на устройство по умолчанию.
   */
  function register(el, channel) {
    if (!el || CHANNELS.indexOf(channel) === -1) return Promise.resolve(false);
    if (elements[channel].indexOf(el) === -1) elements[channel].push(el);
    if (loaded) return applyTo(el, channel);
    return load().then(function () { return applyTo(el, channel); });
  }

  function unregister(el, channel) {
    var i = elements[channel] ? elements[channel].indexOf(el) : -1;
    if (i !== -1) elements[channel].splice(i, 1);
  }

  /** Загрузить выбор оператора с сервера. Безопасно вызывать много раз. */
  function load() {
    if (loadPromise) return loadPromise;
    loadPromise = fetch('/api/audio/outputs?t=' + Date.now())
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var out = (d && d.outputs) || {};
        CHANNELS.forEach(function (ch) {
          sinks[ch] = (out[ch] && out[ch].deviceId) || '';
        });
        loaded = true;
        log('loaded', JSON.stringify(sinks));
        CHANNELS.forEach(applyAll);
        return sinks;
      })
      .catch(function (e) {
        log('load FAILED', e);
        loaded = true;
        return sinks;
      });
    return loadPromise;
  }

  /** Сменить устройство канала локально (без записи на сервер). */
  function setSink(channel, deviceId) {
    if (CHANNELS.indexOf(channel) === -1) return;
    sinks[channel] = deviceId || '';
    applyAll(channel);
  }

  /** Сменить устройство и сохранить выбор на сервере. */
  function saveSink(channel, deviceId, label) {
    setSink(channel, deviceId);
    var fd = new FormData();
    fd.append('channel', channel);
    fd.append('device_id', deviceId || '');
    fd.append('label', label || '');
    return fetch('/api/audio/outputs', { method: 'POST', body: fd })
      .then(function (r) { return r.json(); });
  }

  function getSink(channel) { return sinks[channel] || ''; }

  /**
   * Список доступных выходов: [{deviceId, label}].
   * Если браузер скрыл подписи, один раз просим разрешение на медиа —
   * поток немедленно останавливается, ничего не записывается.
   */
  async function listDevices() {
    var reason = unavailableReason();
    // Пустой список молча выглядел бы как «устройств нет» — бросаем причину,
    // чтобы панель показала оператору, что именно мешает.
    if (reason) { var err = new Error(reason.message); err.code = reason.code; throw err; }
    var devs = await navigator.mediaDevices.enumerateDevices();
    var outs = devs.filter(function (d) { return d.kind === 'audiooutput'; });
    var needLabels = outs.length === 0 || outs.some(function (d) { return !d.label; });
    if (needLabels && navigator.mediaDevices.getUserMedia) {
      try {
        var stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        stream.getTracks().forEach(function (t) { t.stop(); });
        devs = await navigator.mediaDevices.enumerateDevices();
        outs = devs.filter(function (d) { return d.kind === 'audiooutput'; });
      } catch (e) {
        log('permission for device labels denied', e && e.name);
        // Без разрешения Chrome отдаёт единственный безымянный 'default' —
        // выбирать там нечего, поэтому говорим оператору прямо.
        var denied = new Error('Браузер не показывает устройства без доступа к медиа. '
          + 'Разрешите доступ к микрофону для этой страницы и нажмите ⟲ — '
          + 'запись не ведётся, разрешение нужно только чтобы увидеть названия колонок.');
        denied.code = 'no-permission';
        throw denied;
      }
    }
    return outs.map(function (d, i) {
      return {
        deviceId: d.deviceId === 'default' ? '' : d.deviceId,
        label: d.label || ('Устройство ' + (i + 1)),
      };
    });
  }

  window.AudioOutput = {
    CHANNELS: CHANNELS,
    isSupported: isSupported,
    unavailableReason: unavailableReason,
    register: register,
    unregister: unregister,
    applyTo: applyTo,
    load: load,
    setSink: setSink,
    saveSink: saveSink,
    getSink: getSink,
    listDevices: listDevices,
    isLoaded: function () { return loaded; },
  };

  load();
})();
