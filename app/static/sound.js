/*
 * Sound engine (клиент) — Buckshot Roulette IRL.
 *
 * Играет озвучку на экране дилера/стола. Источник правды по файлам и вкл/выкл —
 * бэкенд (`/api/audio/*`): движок стримит `/api/audio/file/<key>` (эффективный
 * файл), выключенное событие отдаёт 204 и не звучит. Смена звука через веб-панель
 * применяется сразу.
 *
 * Детект событий:
 *  - переходы `phase` и смена текущего игрока → фазовые + loop-звуки;
 *  - новые записи `log[]` (по `type` + тексту `message`) → точечные звуки.
 *
 * Автоплей: браузер блокирует звук до первого жеста пользователя. Мы НЕ гейтим
 * заранее — пробуем проиграть, а при отказе (NotAllowedError) показываем баннер
 * «нажмите для звука». Любой клик/нажатие снимает блок.
 *
 * Ключи совпадают с app/sound_config.py / docs/SOUND_EVENTS.md.
 */
(function () {
  var DEBUG = true;
  function log() { if (DEBUG && window.console) console.log.apply(console, ['[sound]'].concat([].slice.call(arguments))); }

  var LOOP_BY_PHASE = {
    lobby: 'ambient_lobby',
    round_start: 'ambient_loading',
    dealer_loading: 'ambient_loading',
    dealer_reloading: 'ambient_loading',
    dealer_items: 'ambient_loading',
    round_over: 'ambient_between_rounds',
    game_over: 'bgm_death',
    // player_turn выбирается динамически (pending → ambient_pending, иначе bgm_main)
  };

  var engine = {
    cfg: {},
    ready: false,
    blocked: false,      // браузер отказал в автоплее — ждём жест
    masterVolume: 0.8,
    initialized: false,  // получили хотя бы один снапшот
    prevPhase: null,
    prevPlayerId: null,
    prevLog: null,
    prevShowShells: null,
    loopKey: null,
    loopAudio: null,
    ver: 0,
  };

  function src(key) { return '/api/audio/file/' + encodeURIComponent(key) + '?v=' + engine.ver; }

  var savedVol = parseFloat(localStorage.getItem('bsr_sound_volume'));
  if (!isNaN(savedVol)) engine.masterVolume = savedVol;

  async function loadConfig() {
    try {
      var r = await fetch('/api/audio/config?t=' + Date.now());
      var d = await r.json();
      var map = {};
      (d.events || []).forEach(function (e) {
        map[e.key] = {
          enabled: e.enabled,
          loop: e.loop,
          volume: e.volume !== undefined ? e.volume : 1.0
        };
      });
      engine.cfg = map;
      engine.ready = true;
      log('config loaded,', Object.keys(map).length, 'events');
    } catch (e) { log('config load FAILED', e); }
  }

  function enabled(key) {
    var c = engine.cfg[key];
    return !c || c.enabled !== false;
  }

  // Пытаемся проиграть; при блокировке автоплея показываем баннер.
  function attempt(audio, key) {
    var p = audio.play();
    if (p && p.catch) {
      p.then(function () { engine.blocked = false; hideBanner(); })
       .catch(function (err) {
         if (err && err.name === 'NotAllowedError') {
           engine.blocked = true; showBanner();
           log('autoplay BLOCKED on', key, '- нужен жест');
         } else {
           log('play error on', key, err && err.name);
         }
       });
    }
  }

  function play(key) {
    if (!enabled(key)) { log('skip (disabled):', key); return; }
    try {
      var a = new Audio(src(key));
      var vol = (engine.cfg[key] && engine.cfg[key].volume !== undefined) ? engine.cfg[key].volume : 1.0;
      a.volume = engine.masterVolume * vol;
      log('play', key, 'volume', a.volume);
      attempt(a, key);
    } catch (e) { log('play throw', key, e); }
  }

  function setLoop(key) {
    if (key === engine.loopKey && engine.loopAudio) return;
    engine.loopKey = key;
    if (engine.loopAudio) { try { engine.loopAudio.pause(); } catch (e) {} engine.loopAudio = null; }
    if (!key || !enabled(key)) return;
    try {
      var a = new Audio(src(key));
      a.loop = true;
      var vol = (engine.cfg[key] && engine.cfg[key].volume !== undefined) ? engine.cfg[key].volume : 1.0;
      a.volume = engine.masterVolume * 0.55 * vol;
      log('loop', key, 'volume', a.volume);
      attempt(a, key);
      engine.loopAudio = a;
    } catch (e) { log('loop throw', key, e); }
  }

  function loopForState(s) {
    if (!s || !s.phase) return null;
    if (s.phase === 'player_turn') return s.pending_shot ? 'ambient_pending' : 'bgm_main';
    return LOOP_BY_PHASE[s.phase] || null;
  }

  function classify(entry) {
    var m = entry.message || '', t = entry.type || '';
    if (t === 'shot') {
      if (m.indexOf('[КУРОК]') === 0) return 'trigger_pull';
      if (m.indexOf('[БОЕВОЙ]') === 0) return m.indexOf('(-2 HP)') !== -1 ? 'shot_live_saw' : 'shot_live';
      if (m.indexOf('[ХОЛОСТОЙ]') === 0) return 'shot_blank';
      if (m.indexOf('испорченного лекарства') !== -1) return 'item_medicine_death';
      if (m.indexOf('выбыл') !== -1) return 'player_dead';
      return null;
    }
    if (t === 'item') {
      if (m.indexOf('наручники') !== -1 && m.indexOf('пропуска') !== -1) return 'handcuff_skip';
      if (m.indexOf('испорченного лекарства') !== -1) return 'item_medicine_death';
      return null;
    }
    if (t === 'round') {
      if (m.indexOf('Новый магазин') !== -1 || m.indexOf('расстреляны') !== -1) return 'new_magazine';
      if (m.indexOf('ВЫИГРАЛ') !== -1) return 'round_win';
      if (m.indexOf('Ничья') !== -1) return 'round_draw';
      if (m.indexOf('ПОБЕДИТЕЛЬ') !== -1) return 'game_over';
      return null;
    }
    if (t === 'system') {
      if (m.indexOf('присоединился') !== -1) return 'player_join';
      if (m.indexOf('покинул') !== -1) return 'player_leave';
      if (m.indexOf('изменил HP') !== -1) return 'hp_adjust';
      if (m.indexOf('Игра завершена дилером') !== -1) return 'force_end_game';
      if (m.indexOf('Раунд завершен дилером') !== -1) return 'force_round_over';
      return null;
    }
    if (t === 'info') {
      if (m.indexOf('дополнительный ход') !== -1) return 'blank_self_extra';
      if (m.indexOf('пропускает ход') !== -1) return 'handcuff_skip';
      return null;
    }
    return null;
  }

  // Новые записи лога = хвост cur после последнего совпадения с prev.
  function newEntries(prev, cur) {
    cur = cur || [];
    if (!cur.length) return [];
    if (!prev || !prev.length) {
      // prev пуст: если это не самый первый снапшот — все текущие новые (капим).
      return engine.initialized ? cur.slice(-4) : [];
    }
    var last = prev[prev.length - 1];
    for (var i = cur.length - 1; i >= 0; i--) {
      if (cur[i].message === last.message && cur[i].type === last.type) return cur.slice(i + 1);
    }
    return cur.slice(-4);
  }

  function onState(s) {
    if (!s) return;
    var phase = s.phase || 'no_game';
    var curPlayerId = s.current_player ? s.current_player.id : null;

    var fresh = newEntries(engine.prevLog, s.log || []);
    if (fresh.length) log('new log entries:', fresh.length);
    fresh.forEach(function (e) { var k = classify(e); if (k) play(k); });

    if (phase !== engine.prevPhase) {
      log('phase', engine.prevPhase, '->', phase);
      if (engine.prevPhase === 'lobby' && phase !== 'lobby' && phase !== 'no_game') play('game_start');
      else if (phase === 'round_start') play(engine.prevPhase === 'round_over' ? 'next_round' : 'round_start');
      if (phase === 'dealer_loading') play('dealer_loading');
      if (phase === 'dealer_reloading') play('dealer_reloading');
      if (phase === 'dealer_items') play('dealer_items');
      if (phase === 'player_turn') play('turn_start');
    } else if (phase === 'player_turn' && curPlayerId && curPlayerId !== engine.prevPlayerId) {
      play('turn_start');
    }

    if (engine.prevShowShells !== null && s.show_shells_to_players !== undefined
        && s.show_shells_to_players !== engine.prevShowShells) play('toggle_shells');

    setLoop(loopForState(s));

    engine.initialized = true;
    engine.prevPhase = phase;
    engine.prevPlayerId = curPlayerId;
    engine.prevLog = s.log || [];
    if (s.show_shells_to_players !== undefined) engine.prevShowShells = s.show_shells_to_players;
  }

  // ── Разблокировка автоплея ──
  function onGesture() {
    if (engine.blocked || !engine.loopAudio) {
      // Перезапустить актуальный loop и снять баннер.
      var want = engine.loopKey;
      engine.loopKey = null;
      setLoop(want);
    }
    engine.blocked = false;
    hideBanner();
  }
  ['pointerdown', 'click', 'keydown', 'touchstart'].forEach(function (ev) {
    document.addEventListener(ev, onGesture, true);
  });

  // ── Баннер «нужен жест» ──
  var banner = null;
  function ensureBanner() {
    if (banner) return banner;
    banner = document.createElement('div');
    banner.textContent = '🔇 Нажмите здесь, чтобы включить звук';
    banner.style.cssText = 'position:fixed;left:12px;bottom:12px;z-index:99999;background:#2a1010;color:#ffb;border:1px solid #a83232;padding:10px 14px;font:13px/1 monospace;border-radius:4px;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.5)';
    banner.onclick = onGesture;
    document.body.appendChild(banner);
    return banner;
  }
  function showBanner() { if (document.body) ensureBanner().style.display = 'block'; }
  function hideBanner() { if (banner) banner.style.display = 'none'; }

  window.SoundEngine = {
    onState: onState,
    play: play,
    reload: async function () { engine.ver++; await loadConfig(); var k = engine.loopKey; engine.loopKey = null; setLoop(k); },
    setVolume: function (v) {
      engine.masterVolume = Math.max(0, Math.min(1, v));
      localStorage.setItem('bsr_sound_volume', String(engine.masterVolume));
      if (engine.loopAudio) {
        var key = engine.loopKey;
        var vol = (engine.cfg[key] && engine.cfg[key].volume !== undefined) ? engine.cfg[key].volume : 1.0;
        engine.loopAudio.volume = engine.masterVolume * 0.55 * vol;
      }
    },
    getVolume: function () { return engine.masterVolume; },
    isEnabled: enabled,
    test: function () { play('ui_click'); },   // ручная проверка из консоли
    _engine: engine,
  };

  log('engine init');
  loadConfig();
})();
