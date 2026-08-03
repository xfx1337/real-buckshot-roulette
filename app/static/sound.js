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
 * Вывод: все звуки идут в канал 'game' модуля AudioOutput (static/audio_output.js) —
 * оператор может направить их на отдельную колонку, оставив звук видео на ТВ.
 *
 * Ключи совпадают с app/sound_config.py / docs/SOUND_EVENTS.md.
 */
(function () {
  var DEBUG = true;
  function log() { if (DEBUG && window.console) console.log.apply(console, ['[sound]'].concat([].slice.call(arguments))); }

  var LOOP_BY_PHASE = {
    lobby: 'ambient_lobby',
    // Зарядка/дозарядка/раздача = «дилер заряжает дробовик» и вход в комнату —
    // играет музыка главного меню (bgm_menu).
    round_start: 'bgm_menu',
    dealer_loading: 'bgm_menu',
    dealer_reloading: 'bgm_menu',
    dealer_items: 'bgm_menu',
    round_over: 'ambient_between_rounds',
    game_over: 'bgm_death',
    // player_turn выбирается динамически (pending → ambient_pending, иначе bgm_main)
  };

  var engine = {
    cfg: {},
    ready: false,
    blocked: false,      // браузер отказал в автоплее — ждём жест
    masterVolume: 0.8,
    duckingEnabled: true,
    ducked: false,
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

  // Новый Audio, привязанный к каналу вывода 'game' (см. static/audio_output.js).
  // Промис применения sink кладём на сам элемент: attempt() дожидается его перед
  // play(), иначе звук стартует на устройстве по умолчанию (setSinkId — async).
  function makeAudio(key) {
    var a = new Audio(src(key));
    if (window.AudioOutput) a._sinkReady = window.AudioOutput.register(a, 'game');
    return a;
  }

  var savedVol = parseFloat(localStorage.getItem('bsr_sound_volume'));
  if (!isNaN(savedVol)) engine.masterVolume = savedVol;
  var savedDucking = localStorage.getItem('bsr_sound_ducking');
  if (savedDucking !== null) engine.duckingEnabled = savedDucking !== 'false';

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
  // Стартуем только после того, как элементу применён выбранный выход, иначе
  // первые звуки уходят на устройство по умолчанию.
  function attempt(audio, key) {
    function start() {
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
    if (audio._sinkReady && audio._sinkReady.then) audio._sinkReady.then(start, start);
    else start();
  }

  // WebAudio-контекст для усиления >1.0 (HTMLAudio.volume режется на 1.0).
  //
  // Внимание: звук, пропущенный через WebAudio, выходит из ctx.destination и
  // ИГНОРИРУЕТ setSinkId самого элемента. Поэтому контекст сам направляется на
  // выбранный канал 'game' через AudioContext.setSinkId (Chrome 110+). Если
  // браузер этого не умеет, а оператор выбрал НЕ дефолтное устройство — путь с
  // усилением отключаем, иначе громкий звук улетит не на ту колонку.
  var audioCtx = null;
  var ctxSink = null;   // sinkId, применённый к контексту
  function getCtx() {
    if (audioCtx) { syncCtxSink(); return audioCtx; }
    var AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    try { audioCtx = new AC(); } catch (e) { audioCtx = null; }
    syncCtxSink();
    return audioCtx;
  }

  function wantedSink() {
    return window.AudioOutput ? window.AudioOutput.getSink('game') : '';
  }

  function ctxCanRoute() {
    var AC = window.AudioContext || window.webkitAudioContext;
    return !!(AC && AC.prototype && typeof AC.prototype.setSinkId === 'function');
  }

  function syncCtxSink() {
    if (!audioCtx || !ctxCanRoute()) return;
    var want = wantedSink();
    if (ctxSink === want) return;
    ctxSink = want;
    try {
      var p = audioCtx.setSinkId(want);
      if (p && p.catch) p.catch(function (e) { log('ctx setSinkId failed', e && e.name); });
    } catch (e) { log('ctx setSinkId throw', e); }
  }

  // Можно ли безопасно применять усиление через WebAudio для текущего вывода.
  function gainPathSafe() {
    var want = wantedSink();
    if (!want) return true;             // дефолтное устройство — маршрут неважен
    return ctxCanRoute();
  }

  function play(key) {
    if (!enabled(key)) { log('skip (disabled):', key); return; }
    if (key === 'shot_live' || key === 'shot_live_saw' || key === 'shot_blank') {
      triggerDucking();
    }
    try {
      var vol = (engine.cfg[key] && engine.cfg[key].volume !== undefined) ? engine.cfg[key].volume : 1.0;
      var target = engine.masterVolume * vol;
      var a = makeAudio(key);
      if (target > 1.0 && gainPathSafe()) {
        // Усиление через WebAudio gain (например, «звук из рая» +40%).
        var ctx = getCtx();
        if (ctx) {
          if (ctx.state === 'suspended') { try { ctx.resume(); } catch (e) {} }
          try {
            var srcNode = ctx.createMediaElementSource(a);
            var gain = ctx.createGain();
            gain.gain.value = target;
            srcNode.connect(gain); gain.connect(ctx.destination);
            log('play(gain)', key, 'gain', target);
            attempt(a, key);
            return;
          } catch (e) { log('gain path failed, fallback', key, e); }
        }
        a.volume = 1.0;   // фолбэк: без WebAudio выше 1.0 не поднять
      } else {
        // Потолок 1.0: HTMLAudio.volume выше единицы бросает исключение, а
        // gain-путь здесь недоступен (нет WebAudio либо он не умеет маршрутизацию
        // на выбранное устройство вывода).
        a.volume = Math.min(1.0, target);
      }
      log('play', key, 'volume', a.volume);
      attempt(a, key);
    } catch (e) { log('play throw', key, e); }
  }

  function updateLoopVolume() {
    if (!engine.loopAudio) return;
    var key = engine.loopKey;
    var vol = (engine.cfg[key] && engine.cfg[key].volume !== undefined) ? engine.cfg[key].volume : 1.0;
    var base = engine.masterVolume * 0.55 * vol;
    engine.loopAudio.volume = Math.min(1.0, engine.ducked ? base * 0.1 : base);
  }

  var duckTimeout = null;
  function triggerDucking() {
    if (!engine.duckingEnabled || !engine.loopAudio) return;
    if (duckTimeout) { clearTimeout(duckTimeout); }
    engine.ducked = true;
    updateLoopVolume();
    log('ducking loop volume');
    duckTimeout = setTimeout(function () {
      engine.ducked = false;
      updateLoopVolume();
      log('restored loop volume');
      duckTimeout = null;
    }, 2000);
  }

  function setLoop(key) {
    if (key === engine.loopKey && engine.loopAudio) {
      if (!enabled(key)) {
        try { engine.loopAudio.pause(); } catch (e) {}
        if (window.AudioOutput) window.AudioOutput.unregister(engine.loopAudio, 'game');
        engine.loopAudio = null;
      } else {
        updateLoopVolume();
      }
      return;
    }
    engine.loopKey = key;
    if (engine.loopAudio) {
      try { engine.loopAudio.pause(); } catch (e) {}
      if (window.AudioOutput) window.AudioOutput.unregister(engine.loopAudio, 'game');
      engine.loopAudio = null;
    }
    if (!key || !enabled(key)) return;
    try {
      var a = makeAudio(key);
      a.loop = true;
      engine.loopAudio = a;
      updateLoopVolume();
      log('loop', key, 'volume', a.volume);
      attempt(a, key);
    } catch (e) { log('loop throw', key, e); engine.loopAudio = null; }
  }

  function loopForState(s) {
    if (!s || !s.phase) return null;
    if (s.phase === 'player_turn') return s.pending_shot ? 'ambient_pending' : 'bgm_main';
    return LOOP_BY_PHASE[s.phase] || null;
  }

  function classify(entry, logList, entryIdx, s) {
    var m = entry.message || '', t = entry.type || '';
    if (t === 'shot') {
      if (m.indexOf('[КУРОК]') === 0) {
        // Физический выстрел: играем звук выстрела СРАЗУ
        var isSaw = s && s.saw_active;
        if (m.indexOf('(БОЕВОЙ)') !== -1 || m.indexOf('(СЕРЕБРЯНЫЙ)') !== -1) {
          return isSaw ? 'shot_live_saw' : 'shot_live';
        }
        if (m.indexOf('(ХОЛОСТОЙ)') !== -1) {
          return 'shot_blank';
        }
        return 'trigger_pull'; // Фолбэк, если не распознали тип
      }
      
      if (m.indexOf('[БОЕВОЙ]') === 0 || m.indexOf('[СЕРЕБРЯНЫЙ]') === 0 || m.indexOf('[ХОЛОСТОЙ]') === 0) {
        // Проверяем, не был ли этот выстрел уже озвучен на этапе [КУРОК]
        var wasPhysical = false;
        if (logList && entryIdx > 0) {
          for (var i = entryIdx - 1; i >= 0; i--) {
            var prevMsg = logList[i].message || '';
            var prevType = logList[i].type || '';
            if (prevType === 'shot') {
              if (prevMsg.indexOf('[КУРОК]') === 0) {
                wasPhysical = true;
                break;
              }
              if (prevMsg.indexOf('[БОЕВОЙ]') === 0 || prevMsg.indexOf('[СЕРЕБРЯНЫЙ]') === 0 || prevMsg.indexOf('[ХОЛОСТОЙ]') === 0) {
                // Встретили предыдущий исход выстрела раньше [КУРОК] — значит, этот выстрел ручной/дилерский
                break;
              }
            }
          }
        }
        if (wasPhysical) {
          // Уже проиграли звук выстрела на [КУРОК], пропускаем дублирование
          return null;
        }
        
        // Для ручных/дилерских выстрелов играем звук как обычно
        if (m.indexOf('[БОЕВОЙ]') === 0) return m.indexOf('(-2 HP)') !== -1 ? 'shot_live_saw' : 'shot_live';
        if (m.indexOf('[СЕРЕБРЯНЫЙ]') === 0) return 'shot_live';
        if (m.indexOf('[ХОЛОСТОЙ]') === 0) return 'shot_blank';
      }
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
    // Звук играет сервер (PortAudio, см. app/audio_engine.py) — здесь молчим,
    // иначе каждое событие прозвучит дважды. Историю всё равно ведём, чтобы
    // возврат в браузерный режим не выстрелил пачкой пропущенных событий.
    if (s.server_sound) {
      if (engine.loopAudio) setLoop(null);
      engine.initialized = true;
      engine.prevPhase = s.phase || 'no_game';
      engine.prevPlayerId = s.current_player ? s.current_player.id : null;
      engine.prevLog = s.log || [];
      if (s.show_shells_to_players !== undefined) engine.prevShowShells = s.show_shells_to_players;
      return;
    }
    var phase = s.phase || 'no_game';
    var curPlayerId = s.current_player ? s.current_player.id : null;

    var fresh = newEntries(engine.prevLog, s.log || []);
    if (fresh.length) log('new log entries:', fresh.length);
    fresh.forEach(function (e) {
      var idx = s.log ? s.log.indexOf(e) : -1;
      var k = classify(e, s.log || [], idx, s);
      if (k) play(k);
    });

    if (phase !== engine.prevPhase) {
      log('phase', engine.prevPhase, '->', phase);
      if (engine.prevPhase === 'lobby' && phase !== 'lobby' && phase !== 'no_game') play('game_start');
      else if (phase === 'round_start') play(engine.prevPhase === 'round_over' ? 'next_round' : 'round_start');
      if (phase === 'dealer_loading') play('dealer_loading');
      if (phase === 'dealer_reloading') play('dealer_reloading');
      if (phase === 'dealer_items') play('dealer_items');
      if (phase === 'round_over') play('heaven');
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
    if (audioCtx && audioCtx.state === 'suspended') { try { audioCtx.resume(); } catch (e) {} }
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
    reload: async function (forceRestartLoop) {
      if (forceRestartLoop) {
        engine.ver++;
      }
      await loadConfig();
      var k = engine.loopKey;
      if (forceRestartLoop) {
        engine.loopKey = null;
      }
      setLoop(k);
    },
    setVolume: function (v) {
      engine.masterVolume = Math.max(0, Math.min(1, v));
      localStorage.setItem('bsr_sound_volume', String(engine.masterVolume));
      updateLoopVolume();
    },
    getVolume: function () { return engine.masterVolume; },
    setDucking: function (enabled) {
      engine.duckingEnabled = !!enabled;
      localStorage.setItem('bsr_sound_ducking', String(engine.duckingEnabled));
    },
    isDuckingEnabled: function () { return engine.duckingEnabled; },
    updateLoopVolume: updateLoopVolume,
    // Оператор сменил устройство вывода — сами элементы перенаправит AudioOutput,
    // здесь остаётся довести до нового выхода WebAudio-контекст усиления.
    onOutputChanged: syncCtxSink,
    isEnabled: enabled,
    test: function () { play('ui_click'); },   // ручная проверка из консоли
    _engine: engine,
  };

  log('engine init');
  loadConfig();
})();
