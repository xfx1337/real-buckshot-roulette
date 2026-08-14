/*
 * The page: draw the handsets, follow the event stream, place calls, and show
 * what is up.
 *
 * State arrives three ways, on purpose. /api/state gives the whole picture
 * when the page loads or the stream reconnects; /api/events carries each
 * change after that; /api/health is polled, because "is the PBX running" is
 * not a thing that emits an event when it stops being true.
 */

const EXTENSIONS = ["101", "102", "103", "104", "105", "106", "107", "108"];

// Extension -> FXS port, fixed by the wiring. Mirrors gateway.PORTS; kept here
// so a card can name its port before the gateway has been asked anything.
const PORT_OF = {
  "101": "0/0", "102": "0/1", "103": "0/2", "104": "0/3",
  "105": "1/0", "106": "1/1", "107": "1/2", "108": "1/3",
};

const linesEl = document.getElementById("lines");
const logEl = document.getElementById("log");
const logEmpty = document.getElementById("log-empty");
const linkEl = document.getElementById("link");
const linkText = document.getElementById("link-text");
const checksEl = document.getElementById("checks");
const stampEl = document.getElementById("health-stamp");
const bannerEl = document.getElementById("maintenance");
const bannerText = document.getElementById("maintenance-text");
const dialog = document.getElementById("dialog");
const dialogExt = document.getElementById("dialog-ext");
const dialogHint = document.getElementById("dialog-hint");
const confirmDialog = document.getElementById("confirm");
const soundSelect = document.getElementById("sound");
const ringInput = document.getElementById("ring");
const ringOut = document.getElementById("ring-out");
const loopInput = document.getElementById("loop");
const toastEl = document.getElementById("toast");

const lines = new Map();   // extension -> latest line state
const calls = new Map();   // extension -> latest call state
const hardware = new Map(); // extension -> what the gateway says about its port
let target = null;         // which handset the dialog is for
let maintenance = false;

const STATE_LABEL = {
  "idle": "трубка на месте",
  "ringing": "звонит",
  "off-hook": "трубка снята",
};

// The gateway's own words for a port, which are not translated at the source
// because they are what its CLI prints and what its manual documents.
const PORT_STATUS = {
  "Idle": "свободен",
  "Busy": "занят",
  "Disconnecting": "разъединяется",
  "Ringing": "звонит",
  "Waiting": "ожидание",
};

const KIND_LABEL = {
  "off-hook": "снята",
  "on-hook": "положена",
  // The call attempt finishing, which is the gateway giving up on the INVITE
  // rather than anybody putting a receiver down. Only the ESP reads the hook.
  "call-ended": "вызов завершён",
  "digit": "цифра",
  "ringing": "звонок",
  "error": "ошибка",
  "info": "инфо",
};

// ── drawing handsets ──────────────────────────────────────────────────

function card(extension) {
  const li = document.createElement("li");
  li.className = "line";
  li.dataset.extension = extension;
  li.innerHTML = `
    <div class="line-top">
      <span><span class="ext"></span><span class="port-tag"></span></span>
      <span class="state"><span class="dot"></span><span class="state-text"></span></span>
    </div>
    <div class="digits"></div>
    <div class="hw"><span class="dot"></span><span class="hw-text">порт не опрошен</span></div>
    <div class="line-actions">
      <button class="primary" data-act="call" type="button">Позвонить</button>
      <button class="ghost" data-act="hangup" type="button"
              title="Завершить текущий разговор через АТС">Сбросить</button>
      <button class="ghost" data-act="clear" type="button"
              title="Принудительно освободить порт на шлюзе">Освободить</button>
      <button class="ghost wide" data-act="onhook" type="button"
              title="Обесточить линию на 6 секунд, чтобы снять замыкание">
        Я положил трубку
      </button>
    </div>
    <label class="check auto-power" title="После каждого отбоя линия обесточивается автоматически">
      <input type="checkbox" class="switch" data-act="autopower">
      <span>Обесточивать после отбоя</span>
    </label>`;
  li.querySelector(".ext").textContent = extension;
  li.querySelector(".port-tag").textContent = `порт ${PORT_OF[extension]}`;
  return li;
}

function draw(extension) {
  const el = linesEl.querySelector(`[data-extension="${extension}"]`);
  if (!el) return;

  const line = lines.get(extension) || { state: "idle", digits: "", direction: "" };
  const busy = calls.get(extension)?.busy === true;

  el.dataset.state = line.state;
  el.querySelector(".digits").textContent = line.digits || "";

  let label = STATE_LABEL[line.state] || line.state;
  // Which side started the call answers the question a bare "off hook" leaves
  // open: whether they rang us or we rang them.
  if (line.state === "off-hook" && line.direction) {
    label += line.direction === "inbound" ? " · звонят нам" : " · мы позвонили";
  }
  if (busy) label = "набор…";
  el.querySelector(".state-text").textContent = label;

  const hw = hardware.get(extension);
  const hwEl = el.querySelector(".hw");
  if (hw) {
    hwEl.dataset.usable = String(hw.usable);
    hwEl.querySelector(".hw-text").textContent =
      `${hw.port}: ${PORT_STATUS[hw.status] || hw.status}`;
  } else {
    hwEl.removeAttribute("data-usable");
    hwEl.querySelector(".hw-text").textContent = "порт не опрошен";
  }

  const callButton = el.querySelector('[data-act="call"]');
  callButton.disabled = busy || maintenance;
  callButton.textContent = busy ? "Набор…" : "Позвонить";
  el.querySelector('[data-act="clear"]').disabled = maintenance;
}

function drawAll() {
  EXTENSIONS.forEach(draw);
}

// ── the feed ──────────────────────────────────────────────────────────

function entry(event, fresh) {
  const li = document.createElement("li");
  li.className = "entry" + (fresh ? " fresh" : "");

  const time = document.createElement("span");
  time.className = "time";
  time.textContent = event.clock;

  const tag = document.createElement("span");
  tag.className = `tag ${event.kind}`;
  tag.textContent = KIND_LABEL[event.kind] || event.kind;

  const what = document.createElement("span");
  what.className = "what";
  if (event.extension && event.extension !== "-") {
    const who = document.createElement("b");
    who.textContent = event.extension;
    what.append(who, " ");
  }
  if (event.kind === "digit") {
    const key = document.createElement("span");
    key.className = "key";
    key.textContent = event.detail;
    what.append("нажата ", key);
  } else {
    what.append(event.detail);
  }

  li.append(time, tag, what);
  return li;
}

function push(event) {
  logEmpty.hidden = true;
  logEl.prepend(entry(event, true));
  while (logEl.children.length > 300) logEl.lastElementChild.remove();
}

// ── talking to the server ─────────────────────────────────────────────

function toast(message, ms = 3600) {
  toastEl.textContent = message;
  toastEl.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => toastEl.classList.remove("show"), ms);
}

function link(state, text) {
  linkEl.className = `status ${state}`;
  linkText.textContent = text;
}

// ── system status ─────────────────────────────────────────────────────

function checkRow(check) {
  const li = document.createElement("li");
  li.className = "check-row";
  li.dataset.state = check.state;

  const dot = document.createElement("span");
  dot.className = "dot";

  const name = document.createElement("span");
  name.className = "check-name";
  name.textContent = check.label;

  const detail = document.createElement("span");
  detail.className = "check-detail";
  // A fix that has to be typed is marked up as a command so it can be copied
  // in one click, rather than selected out of a sentence by hand.
  const command = check.detail.match(/(sudo [^.]+|\.\/scripts\/[^\s]+ \w+)/);
  if (command) {
    const [before, after] = check.detail.split(command[0]);
    const code = document.createElement("code");
    code.textContent = command[0];
    detail.append(before, code, after || "");
  } else {
    detail.textContent = check.detail;
  }

  li.append(dot, name, detail);
  return li;
}

function drawPorts(ports) {
  hardware.clear();
  ports.forEach((port) => hardware.set(port.extension, port));

  let list = document.getElementById("ports");
  if (!list) {
    list = document.createElement("ul");
    list.className = "ports";
    list.id = "ports";
    checksEl.after(list);
  }
  list.replaceChildren();
  ports.forEach((port) => {
    const li = document.createElement("li");
    li.className = "port";
    li.dataset.usable = String(port.usable);
    li.innerHTML = `<span class="dot"></span>
      <span class="port-id"></span><span class="port-status"></span>`;
    li.querySelector(".port-id").textContent = `${port.extension}·${port.port}`;
    li.querySelector(".port-status").textContent =
      PORT_STATUS[port.status] || port.status;
    li.title = `Внутренний ${port.extension}, порт FXS ${port.port}: ${port.status}`;
    list.append(li);
  });
  drawAll();
}

function drawMaintenance(state) {
  maintenance = state.busy === true;
  bannerEl.hidden = !maintenance;
  if (maintenance) {
    bannerText.textContent = state.what === "reboot"
      ? "Шлюз перезагружается. Связь появится примерно через минуту."
      : "Сброс портов шлюза…";
    // A reboot takes about a minute and a reset about twenty seconds. Well
    // past either, the banner is stale rather than informative — the server
    // is the authority, but a lost update must not leave it up forever.
    clearTimeout(drawMaintenance.timer);
    drawMaintenance.timer = setTimeout(() => {
      bannerEl.hidden = true;
      loadHealth();
    }, 240000);
  } else {
    clearTimeout(drawMaintenance.timer);
  }
  document.getElementById("reboot").disabled = maintenance;
  document.getElementById("reset-ports").disabled = maintenance;
  drawAll();
}

async function loadHealth() {
  try {
    const response = await fetch("/api/health");
    // A 404 here means the page and the server disagree about what exists —
    // usually a running web.py that predates an edit, since Flask does not
    // re-import modules while the static files are re-read on every request.
    // Saying so beats leaving a stale banner up with no explanation.
    if (response.status === 404) {
      drawMaintenance({});
      stampEl.textContent = "перезапустите web.py";
      checksEl.replaceChildren(checkRow({
        state: "down", label: "Сервер",
        detail: "Запущена старая версия web.py — остановите его (Ctrl+C) и "
              + "запустите заново: python3 scripts/web.py",
      }));
      return;
    }
    const data = await response.json();

    // The banner first, before anything that renders. It reflects whether the
    // gateway is busy, and that answer must not depend on the rest of this
    // function succeeding — a throw further down used to leave a stale banner
    // on screen with the operation long finished.
    drawMaintenance(data.maintenance || {});

    checksEl.replaceChildren();
    data.checks.forEach((check) => checksEl.append(checkRow(check)));
    drawPorts(data.ports || []);

    stampEl.textContent = new Date(data.at * 1000)
      .toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch (error) {
    // The banner is driven by the server; with no answer it cannot be
    // trusted, so it comes down rather than hanging on stale state.
    drawMaintenance({});
    stampEl.textContent = "сервер недоступен";
  }
}

async function loadState() {
  const response = await fetch("/api/state");
  const data = await response.json();

  lines.clear();
  data.lines.forEach((line) => lines.set(line.extension, line));

  calls.clear();
  Object.entries(data.calls || {}).forEach(([ext, c]) => calls.set(ext, c));

  logEl.replaceChildren();
  // The server keeps the log newest-last; the page shows newest-first.
  data.log.slice().reverse().forEach((e) => logEl.append(entry(e, false)));
  logEmpty.hidden = data.log.length > 0;

  drawAll();
  // Restores the bar after a page reload mid-operation, so a call started
  // before a refresh does not lose its progress display.
  if (data.progress && !data.progress.done) drawProgress(data.progress);
  link(data.connected ? "live" : "down",
       data.connected ? "события идут" : (data.error || "АТС недоступна"));
}

async function loadSounds() {
  const response = await fetch("/api/sounds");
  const data = await response.json();
  soundSelect.replaceChildren();
  if (!data.sounds || data.sounds.length === 0) {
    const option = new Option("нет звуков — положите файл в sounds/", "");
    option.disabled = true;
    soundSelect.append(option);
    return;
  }
  data.sounds.forEach((sound) => {
    soundSelect.append(new Option(`${sound.name} · ${sound.seconds} с`, sound.name));
  });
}

function stream() {
  const source = new EventSource("/api/events");

  source.onopen = () => link("live", "события идут");

  source.onmessage = (message) => {
    const event = JSON.parse(message.data);

    // Progress frames carry no handset event — they only redraw the bar, and
    // putting them in the feed would bury the real events under a step log.
    if (event.kind === "progress") {
      drawProgress(event.progress);
      return;
    }

    push(event);
    pushDial(event);

    // The board's own state is not carried by the event, only the change it
    // implies, so it is applied here rather than refetched per event.
    const line = lines.get(event.extension);
    if (line) {
      if (event.kind === "off-hook") {
        line.state = "off-hook";
        line.direction = event.direction || line.direction;
      } else if (event.kind === "ringing") {
        line.state = "ringing";
        line.direction = event.direction || line.direction;
      } else if (event.kind === "on-hook") {
        line.state = "idle";
        line.digits = "";
        line.direction = "";
      } else if (event.kind === "call-ended") {
        // The gateway stopped ringing. That says nothing about the receiver,
        // which the ESP reports separately and which is often lifted a moment
        // after the bells stop — so a handset already off the hook keeps that
        // state and only a resting one goes back to idle.
        if (line.state !== "off-hook") {
          line.state = "idle";
          line.digits = "";
          line.direction = "";
        }
      } else if (event.kind === "digit") {
        line.digits = (line.digits || "") + event.detail;
      }
    }

    // A call's outcome and a finished reboot both arrive as info/error lines.
    // Both change things this page cannot derive from the event alone, so it
    // re-reads rather than guessing.
    if (event.kind === "error" || event.kind === "info") {
      fetch("/api/state").then((r) => r.json()).then((data) => {
        calls.clear();
        Object.entries(data.calls || {}).forEach(([ext, c]) => calls.set(ext, c));
        drawAll();
      });
      loadHealth();
    }

    if (event.extension && event.extension !== "-") draw(event.extension);
  };

  source.onerror = () => {
    link("down", "переподключение…");
    // EventSource retries on its own; the state refetch on the next open is
    // what repairs anything missed while it was down.
    source.addEventListener("open", () => loadState(), { once: true });
  };
}

// ── handset buttons ───────────────────────────────────────────────────

// The per-handset switch is a checkbox, so it reports through change rather
// than click.
linesEl.addEventListener("change", async (changeEvent) => {
  const control = changeEvent.target;
  if (control.dataset.act !== "autopower") return;
  const extension = control.closest(".line").dataset.extension;
  try {
    const response = await fetch("/api/auto-power", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ extension, enabled: control.checked }),
    });
    const data = await response.json();
    toast(response.ok
      ? `${extension}: обесточивание после отбоя ${control.checked ? "включено" : "выключено"}`
      : data.error);
  } catch (error) {
    toast(String(error));
  }
});

linesEl.addEventListener("click", async (clickEvent) => {
  const button = clickEvent.target.closest("button");
  if (!button) return;
  const extension = button.closest(".line").dataset.extension;

  if (button.dataset.act === "call") {
    target = extension;
    dialogExt.textContent = extension;
    dialogHint.textContent = "Перед вызовом порт освобождается, это занимает пару секунд.";
    await loadSounds();
    dialog.showModal();
    return;
  }

  // Two ways to end a call, and they are not the same thing. "Сбросить" asks
  // Asterisk to hang the channel up, which is how a call normally ends.
  // "Освободить" cycles the FXS port on the gateway — the repair for a port
  // that will not let go, used when the first one has nothing to hang up.
  if (button.dataset.act === "hangup") {
    button.disabled = true;
    try {
      const response = await fetch("/api/hangup-call", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ extension }),
      });
      const data = await response.json();
      toast(response.ok ? `${extension}: вызов завершён` : data.error);
    } catch (error) {
      toast(String(error));
    } finally {
      button.disabled = false;
    }
    return;
  }

  // For a handset whose line stays shorted after the receiver is down: holds
  // the line dead long enough for the loop to read open, which a one-second
  // release does not always achieve.
  if (button.dataset.act === "onhook") {
    button.disabled = true;
    const label = button.textContent;
    button.textContent = "Обесточиваю…";
    try {
      const response = await fetch("/api/on-hook", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ extension, seconds: 6 }),
      });
      const data = await response.json();
      toast(response.ok
        ? (data.ok ? `${extension}: линия свободна`
                   : `${extension}: порт остался ${data.status}`)
        : data.error, 5000);
      loadHealth();
    } catch (error) {
      toast(String(error));
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
    return;
  }

  if (button.dataset.act === "clear") {
    button.disabled = true;
    try {
      const response = await fetch("/api/hangup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ extension }),
      });
      const data = await response.json();
      toast(response.ok ? `${extension}: порт освобождён` : data.error);
      loadHealth();
    } catch (error) {
      toast(String(error));
    } finally {
      button.disabled = false;
    }
  }
});

ringInput.addEventListener("input", () => {
  ringOut.textContent = `${ringInput.value} с`;
});

document.getElementById("call-form").addEventListener("submit", async (submitEvent) => {
  // The dialog closes itself on either button; only the Call one places a call.
  if (submitEvent.submitter?.value !== "call") return;
  if (!soundSelect.value) {
    submitEvent.preventDefault();
    dialogHint.textContent = "Нечего играть. Положите mp3 или wav в sounds/.";
    return;
  }

  const body = {
    extension: target,
    sound: soundSelect.value,
    loop: loopInput.checked,
    ring: Number(ringInput.value),
  };

  // Marked busy here rather than waiting for the server's reply, so a second
  // press cannot get through in the gap.
  calls.set(target, { busy: true });
  draw(target);

  try {
    const response = await fetch("/api/call", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) {
      calls.delete(target);
      draw(target);
      toast(data.error || "вызов отклонён");
    } else {
      toast(`звоним на ${data.extension}, играет ${data.sound}`);
    }
  } catch (error) {
    calls.delete(target);
    draw(target);
    toast(String(error));
  }
});

// ── maintenance buttons ───────────────────────────────────────────────

document.getElementById("reset-ports").addEventListener("click", async () => {
  try {
    const response = await fetch("/api/reset-ports", { method: "POST" });
    const data = await response.json();
    toast(response.ok ? "сброс всех портов, около 20 секунд" : data.error);
    loadHealth();
  } catch (error) {
    toast(String(error));
  }
});

document.getElementById("reboot").addEventListener("click", () => {
  confirmDialog.showModal();
});

document.getElementById("confirm-form").addEventListener("submit", async (submitEvent) => {
  if (submitEvent.submitter?.value !== "reboot") return;
  try {
    const response = await fetch("/api/reboot", { method: "POST" });
    const data = await response.json();
    toast(response.ok ? "шлюз перезагружается" : data.error);
    loadHealth();
  } catch (error) {
    toast(String(error));
  }
});

document.getElementById("refresh").addEventListener("click", loadHealth);

// ── automatic release ─────────────────────────────────────────────────

async function loadWatchdog() {
  try {
    const response = await fetch("/api/watchdog");
    const data = await response.json();
    document.getElementById("watchdog-on").checked = data.enabled === true;
    const released = data.watch.reduce((sum, w) => sum + w.releases, 0);
    if (released) {
      document.getElementById("watchdog-note").textContent =
        `Освобождений с момента запуска: ${released}. `
      + "Повторяющиеся сбросы означают, что замыкание в линии не устранено.";
    }
  } catch (error) {
    // The switch simply stays as it was; the server is the authority and the
    // next load will correct it.
  }
}

// Which handsets de-energise their line after every call. Restored on load
// so the switches match the server rather than resetting to off.
async function loadAutoPower() {
  try {
    const response = await fetch("/api/auto-power");
    const data = await response.json();
    (data.extensions || []).forEach((extension) => {
      const control = linesEl.querySelector(
        `[data-extension="${extension}"] [data-act="autopower"]`);
      if (control) control.checked = true;
    });
  } catch (error) {
    // Switches stay as drawn; the next load corrects them.
  }
}

document.getElementById("watchdog-on").addEventListener("change", async (changeEvent) => {
  const on = changeEvent.target.checked;
  try {
    const response = await fetch("/api/watchdog", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: on }),
    });
    const data = await response.json();
    toast(response.ok
      ? (on ? "автосброс включён" : "автосброс выключен")
      : data.error);
  } catch (error) {
    toast(String(error));
  }
});

document.getElementById("clear").addEventListener("click", () => {
  logEl.replaceChildren();
  logEmpty.hidden = false;
});

// ══ progress ══════════════════════════════════════════════════════════

const progressEl = document.getElementById("progress");
const progressSteps = document.getElementById("progress-steps");
const progressFill = document.getElementById("progress-fill");

const STEP_MARK = { waiting: "", running: "", ok: "", fail: "", skip: "" };

function drawProgress(progress) {
  if (!progress || !progress.steps || !progress.steps.length) {
    progressEl.hidden = true;
    return;
  }
  progressEl.hidden = false;
  document.getElementById("progress-title").textContent = progress.title;
  progressEl.dataset.done = progress.done ? (progress.ok ? "ok" : "fail") : "";

  const total = progress.steps.length;
  const settled = progress.steps.filter((s) => s.state !== "waiting" && s.state !== "running").length;
  // A running step counts as half: it shows movement without claiming the
  // step finished, which matters when one step takes most of the time.
  const running = progress.steps.filter((s) => s.state === "running").length;
  progressFill.style.width = `${Math.round(((settled + running * 0.5) / total) * 100)}%`;

  progressSteps.replaceChildren();
  progress.steps.forEach((step) => {
    const li = document.createElement("li");
    li.className = "step";
    li.dataset.state = step.state;
    li.innerHTML = `<span class="step-mark"></span>
                    <span class="step-label"></span>
                    <span class="step-detail"></span>`;
    li.querySelector(".step-label").textContent = step.label;
    li.querySelector(".step-detail").textContent = step.detail || "";
    progressSteps.append(li);
  });

  // A finished job stays on screen briefly so the last step can be read,
  // then clears itself rather than sitting there as stale state.
  clearTimeout(drawProgress.timer);
  if (progress.done) {
    drawProgress.timer = setTimeout(() => { progressEl.hidden = true; }, 12000);
  }
}

document.getElementById("progress-hide").addEventListener("click", () => {
  progressEl.hidden = true;
});

// ══ tabs ══════════════════════════════════════════════════════════════

// Each view loads its data when first opened, and refreshes on request
// rather than on a timer: the gateway views cost a telnet login each, and
// polling them from a background tab would burn the device's few sessions.
const LOADERS = {
  panel: () => loadPanel(),
  gateway: () => loadGateway(),
  pbx: () => loadPbx(),
  dial: () => { loadDialSlots(); loadDialSounds(); },
  tools: () => loadDiagnostics(),
};

document.getElementById("tabs").addEventListener("click", (clickEvent) => {
  const tab = clickEvent.target.closest(".tab");
  if (!tab) return;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("is-on", t === tab));
  document.querySelectorAll(".view").forEach((v) => {
    v.classList.toggle("is-on", v.id === `view-${tab.dataset.tab}`);
  });
  LOADERS[tab.dataset.tab]?.();
});

// ══ indicator panel view ══════════════════════════════════════════════

async function loadPanel() {
  const rows = document.getElementById("led-rows");
  const grid = document.getElementById("fxs-leds");
  const access = document.getElementById("access-rows");
  rows.innerHTML = '<li class="note">опрос шлюза…</li>';

  let data;
  try {
    const response = await fetch("/api/panel");
    data = await response.json();
    if (!response.ok) throw new Error(data.error || "шлюз не отвечает");
  } catch (error) {
    rows.innerHTML = `<li class="note">${error.message}</li>`;
    return;
  }

  rows.replaceChildren();
  data.leds.forEach((led) => {
    const li = document.createElement("li");
    li.className = "led-row";
    // lamp/blink mirror the physical LED; state is the verdict, shown on the
    // row's edge rather than in the lamp's colour.
    li.dataset.lamp = led.lamp;
    li.dataset.blink = led.blink;
    li.dataset.state = led.state;
    li.innerHTML = `<span class="lamp"></span>
      <span class="led-name"></span>
      <span class="led-value${led.inferred ? " inferred" : ""}"></span>
      <span class="led-expect"></span>`;
    li.querySelector(".led-name").textContent = led.label;
    li.querySelector(".led-value").textContent = led.value;
    // The reference column: what the LED on the case should look like, and
    // what it means when it does not.
    li.querySelector(".led-expect").textContent =
      led.state === "ok" ? led.expect : `${led.expect}. ${led.wrong}`;
    rows.append(li);
  });

  grid.replaceChildren();
  data.fxs.forEach((port) => {
    const div = document.createElement("div");
    div.className = "fxs-led led-row";
    div.dataset.lamp = port.lamp;
    div.dataset.blink = port.blink;
    div.dataset.state = port.state;
    div.innerHTML = `<span class="lamp"></span>
      <span class="led-name"></span>
      <span class="fxs-meaning"></span>`;
    div.querySelector(".led-name").textContent =
      `FXS ${port.port} · ${port.extension}`;
    div.querySelector(".fxs-meaning").textContent = `${port.led} — ${port.meaning}`;
    grid.append(div);
  });

  access.replaceChildren();
  [
    { label: "IP-адрес шлюза", detail: data.host },
    { label: "Подключение", detail: `${data.telnet}   (логин ${data.user})` },
    { label: "LAN0", detail: `${data.lan0.speed || 0} Мбит/с ${data.lan0.duplex || ""}`
                           + `, ошибок: ${data.lan0.errors ?? 0}` },
    { label: "Веб-интерфейс АТС", detail: window.location.origin },
  ].forEach((entry) => {
    access.append(checkRow({ state: "ok", label: entry.label, detail: entry.detail }));
  });

  document.getElementById("panel-stamp").textContent =
    new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

document.getElementById("panel-refresh").addEventListener("click", loadPanel);

// ══ gateway view ══════════════════════════════════════════════════════

async function loadGateway() {
  const rows = document.getElementById("port-rows");
  const peerRows = document.getElementById("peer-rows");
  rows.innerHTML = '<tr><td colspan="6" class="muted">опрос шлюза…</td></tr>';

  let data;
  try {
    const response = await fetch("/api/admin/ports");
    data = await response.json();
    if (!response.ok) throw new Error(data.error || "шлюз не отвечает");
  } catch (error) {
    rows.innerHTML = `<tr><td colspan="6" class="muted">${error.message}</td></tr>`;
    return;
  }

  rows.replaceChildren();
  data.ports.forEach((port) => {
    const tr = document.createElement("tr");
    if (port.shutdown) tr.className = "is-shut";
    const state = port.usable ? "ok" : (port.status === "Busy" ? "warn" : "bad");
    tr.innerHTML = `
      <td class="mono">${port.extension}</td>
      <td class="mono">${port.port}</td>
      <td><span class="badge ${state}">${PORT_STATUS[port.status] || port.status}</span>
          ${port.shutdown ? '<span class="badge">выключен</span>' : ""}</td>
      <td>${port.connection}</td>
      <td>${port.polarity_inverse ? '<span class="badge bad">включена</span>' : "нет"}</td>
      <td><div class="row-actions">
        <button class="ghost small" data-act="settings" data-port="${port.port}">Параметры</button>
        <button class="ghost small" data-act="probe" data-port="${port.port}">Проверить</button>
        <button class="ghost small" data-act="toggle" data-port="${port.port}"
                data-up="${port.shutdown ? "1" : "0"}">${port.shutdown ? "Включить" : "Выключить"}</button>
      </div></td>`;
    rows.append(tr);
  });

  peerRows.replaceChildren();
  const options = data.ports.map((p) => p.port);
  data.peers.forEach((peer) => {
    const tr = document.createElement("tr");
    const select = options
      .map((p) => `<option value="${p}"${p === peer.port ? " selected" : ""}>${p}</option>`)
      .join("");
    tr.innerHTML = `
      <td class="mono">${peer.tag}</td>
      <td class="mono">${peer.pattern}</td>
      <td class="mono">${peer.port}</td>
      <td><div class="row-actions">
        <select data-peer="${peer.tag}">${select}</select>
        <button class="ghost small" data-act="repeer" data-tag="${peer.tag}">Применить</button>
      </div></td>`;
    peerRows.append(tr);
  });
}

document.getElementById("view-gateway").addEventListener("click", async (clickEvent) => {
  const button = clickEvent.target.closest("button");
  if (!button) return;
  const port = button.dataset.port;

  if (button.dataset.act === "settings") return openPortSheet(port);

  if (button.dataset.act === "probe") {
    button.disabled = true;
    try {
      const response = await fetch(`/api/admin/probe/${port}`);
      const data = await response.json();
      toast(response.ok
        ? `${port}: ${data.reason}${data.fix ? " — " + data.fix : ""}`
        : data.error, 6000);
    } finally {
      button.disabled = false;
    }
    return;
  }

  if (button.dataset.act === "toggle") {
    const up = button.dataset.up === "1";
    button.disabled = true;
    try {
      const response = await fetch(`/api/admin/port/${port}/state`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ up }),
      });
      const data = await response.json();
      toast(response.ok ? `${port}: ${up ? "включён" : "выключен"}` : data.error);
      loadGateway();
    } finally {
      button.disabled = false;
    }
    return;
  }

  if (button.dataset.act === "repeer") {
    const tag = button.dataset.tag;
    const select = document.querySelector(`select[data-peer="${tag}"]`);
    button.disabled = true;
    try {
      const response = await fetch("/api/admin/dial-peer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag: Number(tag), port: select.value }),
      });
      const data = await response.json();
      toast(response.ok ? `dial-peer ${tag} → порт ${select.value}` : data.error, 6000);
      loadGateway();
    } finally {
      button.disabled = false;
    }
  }
});

// ── port settings sheet ───────────────────────────────────────────────

const portSheet = document.getElementById("port-sheet");

async function openPortSheet(port) {
  const fields = document.getElementById("port-fields");
  fields.innerHTML = '<p class="note">чтение параметров…</p>';
  document.getElementById("port-sheet-id").textContent = port;
  document.getElementById("port-hint").textContent = "";
  portSheet.showModal();

  const response = await fetch(`/api/admin/port/${port}`);
  const data = await response.json();
  if (!response.ok) {
    fields.innerHTML = `<p class="note">${data.error}</p>`;
    return;
  }
  document.getElementById("port-sheet-ext").textContent = data.detail.extension;

  fields.replaceChildren();
  data.parameters.forEach((parameter) => {
    const current = data.detail[parameter.key];
    const wrap = document.createElement("div");
    wrap.className = "setting";

    let control;
    if (parameter.kind === "flag") {
      control = document.createElement("input");
      control.type = "checkbox";
      control.className = "switch";
      control.checked = current === true;
    } else if (parameter.kind === "choice") {
      control = document.createElement("select");
      parameter.choices.forEach((choice) => {
        control.append(new Option(choice, choice, false, choice === current));
      });
    } else {
      control = document.createElement("input");
      control.type = "number";
      control.min = parameter.min;
      control.max = parameter.max;
      control.value = current ?? 0;
    }
    control.dataset.key = parameter.key;
    control.dataset.port = port;

    const top = document.createElement("div");
    top.className = "setting-top";
    const label = document.createElement("span");
    label.className = "setting-label";
    label.textContent = parameter.label + (parameter.unit ? `, ${parameter.unit}` : "");
    top.append(label, control);
    wrap.append(top);

    if (parameter.help) {
      const help = document.createElement("span");
      help.className = "setting-help";
      help.textContent = parameter.help;
      wrap.append(help);
    }
    fields.append(wrap);
  });
}

// Applied on change rather than behind a Save button: each parameter is one
// CLI command, and batching them would hide which one failed.
document.getElementById("port-fields").addEventListener("change", async (changeEvent) => {
  const control = changeEvent.target;
  if (!control.dataset.key) return;
  const value = control.type === "checkbox" ? control.checked
              : control.type === "number" ? Number(control.value)
              : control.value;
  const hint = document.getElementById("port-hint");
  control.disabled = true;
  try {
    const response = await fetch(`/api/admin/port/${control.dataset.port}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: control.dataset.key, value }),
    });
    const data = await response.json();
    hint.textContent = response.ok ? `применено: ${data.command}` : data.error;
    if (response.ok) loadGateway();
  } catch (error) {
    hint.textContent = String(error);
  } finally {
    control.disabled = false;
  }
});

document.getElementById("save-flash").addEventListener("click", () => {
  document.getElementById("confirm-save").showModal();
});

document.getElementById("confirm-save-form").addEventListener("submit", async (submitEvent) => {
  if (submitEvent.submitter?.value !== "save") return;
  const response = await fetch("/api/admin/save", { method: "POST" });
  const data = await response.json();
  toast(response.ok ? "конфигурация сохранена во flash" : data.error, 5000);
});

// ══ PBX view ══════════════════════════════════════════════════════════

async function loadPbx() {
  const checks = document.getElementById("pbx-checks");
  const dialplan = document.getElementById("dialplan-rows");
  const channels = document.getElementById("channel-rows");

  const [healthResponse, pbxResponse] = await Promise.all([
    fetch("/api/health"), fetch("/api/pbx"),
  ]);
  const healthData = await healthResponse.json();
  const pbxData = await pbxResponse.json();

  checks.replaceChildren();
  healthData.checks
    .filter((c) => ["pbx", "ami", "network"].includes(c.name))
    .forEach((c) => checks.append(checkRow(c)));
  checks.append(checkRow({
    name: "trunk", label: "Транк до шлюза", state: pbxData.trunk === "доступен" ? "ok" : "warn",
    detail: pbxData.trunk,
  }));

  dialplan.replaceChildren();
  (pbxData.dialplan || []).forEach((entry) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="mono">${entry.pattern}</td><td>${entry.note}</td>`;
    dialplan.append(tr);
  });

  channels.replaceChildren();
  if (!pbxData.channels.length) {
    const li = document.createElement("li");
    li.className = "check-row placeholder";
    li.textContent = "нет активных вызовов";
    channels.append(li);
  } else {
    pbxData.channels.forEach((line) => {
      const li = document.createElement("li");
      li.className = "check-row";
      li.innerHTML = `<span class="dot"></span><span class="check-name">канал</span>
                      <span class="check-detail mono">${line}</span>`;
      channels.append(li);
    });
  }
}

// The programmable numbers moved to the rotary telephone tab, where they sit
// beside the dial that calls them. See loadDialSlots() below.

document.getElementById("pbx-refresh").addEventListener("click", loadPbx);
document.getElementById("ports-refresh").addEventListener("click", loadGateway);

document.getElementById("dialplan-reload").addEventListener("click", async () => {
  const response = await fetch("/api/pbx/reload", { method: "POST" });
  const data = await response.json();
  toast(response.ok ? "план набора перезагружен" : data.error);
  loadPbx();
});

// ══ diagnostics view ══════════════════════════════════════════════════

async function loadDiagnostics() {
  const holder = document.getElementById("diag-buttons");
  if (holder.children.length) return;          // built once
  const response = await fetch("/api/admin/diagnostics");
  const data = await response.json();
  data.available.forEach((entry) => {
    const button = document.createElement("button");
    button.className = "ghost small";
    button.textContent = entry.label;
    button.title = entry.command;
    button.dataset.diag = entry.key;
    holder.append(button);
  });
}

document.getElementById("diag-buttons").addEventListener("click", async (clickEvent) => {
  const button = clickEvent.target.closest("button");
  if (!button) return;
  const output = document.getElementById("diag-output");
  output.textContent = "выполняется…";
  try {
    const response = await fetch(`/api/admin/diagnostics/${button.dataset.diag}`);
    const data = await response.json();
    output.textContent = response.ok ? (data.text || "(пусто)") : data.error;
  } catch (error) {
    output.textContent = String(error);
  }
});

// ══ rotary telephone view ═════════════════════════════════════════════
//
// Two directions on one tab. Calling the handset goes through the same
// /api/call the board tab uses, so a call started here shows up there and in
// the progress bar exactly as one started from a handset card.
//
// The numbers below it are the other direction: what the dial can ask for.
// Which numbers exist is the server's answer, not a constant here — the
// dialplan matches the whole range with one pattern and the database decides
// which of them are real, so this list is rebuilt from /api/slots rather than
// assumed.

const dialSlotRows = document.getElementById("dial-slot-rows");
const dialLog = document.getElementById("dial-log");
const dialLogEmpty = document.getElementById("dial-log-empty");

// Held between loads so adding a number does not need a second fetch to fill
// its sound list.
let dialSounds = [];

function soundOptions(selected) {
  return ['<option value="">— не назначен —</option>']
    .concat(dialSounds.map((s) =>
      `<option value="${s.name}"${s.name === selected ? " selected" : ""}>`
      + `${s.name} · ${s.seconds} с</option>`))
    .join("");
}

async function loadDialSlots() {
  const stamp = document.getElementById("dial-slots-stamp");
  try {
    const response = await fetch("/api/slots");
    const data = await response.json();
    dialSounds = data.sounds || [];

    dialSlotRows.replaceChildren();

    if (!dialSounds.length) {
      dialSlotRows.innerHTML =
        '<tr><td colspan="3" class="muted">нет звуков — положите файл в sounds/</td></tr>';
    } else if (!data.slots.length) {
      dialSlotRows.innerHTML =
        '<tr><td colspan="3" class="muted">ни одного номера — добавьте ниже</td></tr>';
    } else {
      data.slots.forEach((slot) => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td class="mono">${slot.number}</td>
          <td>${slot.sound
                ? `<span class="badge ok">${slot.sound}</span>`
                : '<span class="badge">пусто</span>'}</td>
          <td><div class="row-actions">
            <select data-dial-slot="${slot.number}">${soundOptions(slot.sound)}</select>
            <button class="ghost small" data-act="dial-set"
                    data-number="${slot.number}">Применить</button>
            <button class="ghost small" data-act="dial-del"
                    data-number="${slot.number}">Убрать</button>
          </div></td>`;
        dialSlotRows.append(tr);
      });
    }

    // Only numbers the dialplan's pattern actually covers are offered, so a
    // number chosen here cannot turn out to be one the PBX ignores.
    const newNumber = document.getElementById("dial-new-number");
    newNumber.replaceChildren();
    (data.free || []).forEach((n) => newNumber.append(new Option(n, n)));
    if (!(data.free || []).length) {
      const option = new Option(
        `свободных номеров нет (${data.range.first}–${data.range.last})`, "");
      option.disabled = true;
      newNumber.append(option);
    }

    document.getElementById("dial-new-sound").innerHTML = soundOptions("");
    stamp.textContent = `${data.slots.length} из ${data.range.first}–${data.range.last}`;
  } catch (error) {
    dialSlotRows.innerHTML = `<tr><td colspan="3" class="muted">${error}</td></tr>`;
  }
}

async function setDialSlot(number, sound) {
  const response = await fetch("/api/slots", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ number, sound }),
  });
  const data = await response.json();
  toast(response.ok
    ? (data.sound ? `${number} → ${data.sound}` : `${number} убран`)
    : data.error);
  await loadDialSlots();
}

document.getElementById("view-dial").addEventListener("click", async (clickEvent) => {
  const button = clickEvent.target.closest("[data-act]");
  if (!button) return;
  const number = button.dataset.number;
  button.disabled = true;
  try {
    if (button.dataset.act === "dial-set") {
      const select = document.querySelector(`select[data-dial-slot="${number}"]`);
      await setDialSlot(number, select.value);
    } else if (button.dataset.act === "dial-del") {
      // An empty sound is how a number is removed: the dialplan's pattern
      // still matches it, but with nothing in the database the call takes the
      // branch that says so rather than playing anything.
      await setDialSlot(number, "");
    }
  } catch (error) {
    toast(String(error));
  } finally {
    button.disabled = false;
  }
});

document.getElementById("dial-add").addEventListener("click", async (clickEvent) => {
  const number = document.getElementById("dial-new-number").value;
  const sound = document.getElementById("dial-new-sound").value;
  if (!number) { toast("нет свободного номера"); return; }
  if (!sound) { toast("выберите звук"); return; }
  const button = clickEvent.currentTarget;
  button.disabled = true;
  try {
    await setDialSlot(number, sound);
  } catch (error) {
    toast(String(error));
  } finally {
    button.disabled = false;
  }
});

document.getElementById("dial-slots-refresh").addEventListener("click", loadDialSlots);

// ── calling the handset from here ─────────────────────────────────────

const dialRing = document.getElementById("dial-ring");
const dialRingOut = document.getElementById("dial-ring-out");
dialRing.addEventListener("input", () => {
  dialRingOut.textContent = `${dialRing.value} с`;
});

document.getElementById("dial-call-go").addEventListener("click", async (clickEvent) => {
  const extension = document.getElementById("dial-ext").value;
  const sound = document.getElementById("dial-sound").value;
  if (!sound) { toast("выберите звук"); return; }
  const button = clickEvent.currentTarget;
  button.disabled = true;
  try {
    const response = await fetch("/api/call", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        extension,
        sound,
        loop: document.getElementById("dial-loop").checked,
        ring: Number(dialRing.value),
      }),
    });
    const data = await response.json();
    toast(response.ok ? `${extension}: вызов пошёл` : data.error);
  } catch (error) {
    toast(String(error));
  } finally {
    button.disabled = false;
  }
});

document.getElementById("dial-hangup").addEventListener("click", async (clickEvent) => {
  const extension = document.getElementById("dial-ext").value;
  const button = clickEvent.currentTarget;
  button.disabled = true;
  try {
    const response = await fetch("/api/hangup-call", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ extension }),
    });
    const data = await response.json();
    toast(response.ok ? `${extension}: вызов завершён` : data.error);
  } catch (error) {
    toast(String(error));
  } finally {
    button.disabled = false;
  }
});

// The sound list is the same library the call dialog offers; loaded here as
// well so the tab is usable without opening that dialog first.
async function loadDialSounds() {
  const select = document.getElementById("dial-sound");
  try {
    const response = await fetch("/api/sounds");
    const data = await response.json();
    select.replaceChildren();
    if (!data.sounds || !data.sounds.length) {
      const option = new Option("нет звуков — положите файл в sounds/", "");
      option.disabled = true;
      select.append(option);
      return;
    }
    data.sounds.forEach((sound) => {
      select.append(new Option(`${sound.name} · ${sound.seconds} с`, sound.name));
    });
  } catch (error) {
    toast(String(error));
  }
}

// ── what the reader is doing ──────────────────────────────────────────
//
// Fed from the same stream as the board's log, narrowed to the handset this
// tab is pointed at and to the kinds a dial produces. Reusing entry() keeps
// the two logs reading identically.

function pushDial(event) {
  if (!["off-hook", "on-hook", "digit"].includes(event.kind)) return;
  if (event.extension !== document.getElementById("dial-ext").value) return;
  dialLogEmpty.hidden = true;
  dialLog.prepend(entry(event, true));
  while (dialLog.children.length > 200) dialLog.lastElementChild.remove();
}

document.getElementById("dial-clear").addEventListener("click", () => {
  dialLog.replaceChildren();
  dialLogEmpty.hidden = false;
});

// Switching which handset the tab watches empties the log rather than leaving
// another set's events under the new heading.
document.getElementById("dial-ext").addEventListener("change", () => {
  dialLog.replaceChildren();
  dialLogEmpty.hidden = false;
});

// ── start ─────────────────────────────────────────────────────────────

EXTENSIONS.forEach((extension) => {
  document.getElementById("dial-ext").append(new Option(extension, extension));
});

EXTENSIONS.forEach((extension) => linesEl.append(card(extension)));
drawAll();
// The banner starts down and is raised only by a server that says the
// gateway is busy. Asserting it here means a failure anywhere in the first
// render cannot leave it showing.
bannerEl.hidden = true;
loadState().catch(() => link("down", "сервер недоступен"));
loadHealth();
loadWatchdog();
loadAutoPower();
stream();

// Polled, because nothing emits an event when Asterisk stops running or an
// address disappears. Rescheduled after each pass rather than run on a fixed
// interval: the rate depends on whether the gateway is mid-reboot, and
// setInterval would fix the rate at whatever was true when the page loaded.
// Chaining also keeps a slow check from overlapping the next one.
(function poll() {
  setTimeout(async () => {
    await loadHealth();
    poll();
  }, maintenance ? 3000 : 10000);
})();
