// Reads a TA-1132 rotary telephone and reports what it does over Wi-Fi.
//
// The TA-1132 signals in two places, both of them plain switch contacts:
//
//   the hook switch   closed while the receiver is lifted
//   НИ, the impulse   opens and closes once per unit of the digit dialled,
//                     as the disc returns under its spring
//
// A digit is a train of those pulses at the Soviet standard ten per second:
// one pulse is 1, nine are 9, and ten are 0. Digits are separated by the
// pause while the caller's finger returns to the disc.
//
// So the whole job is counting edges and deciding where one train ends and
// the next begins. Both contacts are read by interrupt and debounced there;
// the loop only ever sees settled state, and the counting is what the ISR
// leaves behind.
//
// Every event is queued and posted by a task of its own, so a server that
// is slow, or gone, cannot make the firmware miss a pulse.
//
// Wiring: both contacts are dry, one side to the pin and the other to GND.
// The receiver's own line and its bell stay on the gateway's FXS port and
// are not touched here — the ESP shares only ground with the telephone's
// contacts and runs from its own supply.

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>

#include "config.h"

// One thing to tell the server: an event of a kind the server already knows,
// and whatever detail belongs to it. Declared before anything else so the
// .ino build, which hoists prototypes above the file's own definitions,
// still has the type when it reaches them.
struct Event {
    char kind[12];    // off-hook | on-hook | digit | number
    char detail[16];  // the digit, the number, or empty
};

// ── what the loop and the ISRs share ────────────────────────────────────
//
// Both ISRs run on the same core as the loop and can interrupt it between
// any two instructions. Anything they touch is volatile, and the loop reads
// multi-field state with interrupts masked so it cannot catch an update
// half-applied.

// Pulses counted in the train currently being dialled.
static volatile uint32_t pulseCount = 0;

// millis() at the last accepted pulse edge — what the loop measures the
// inter-digit gap from.
static volatile uint32_t lastPulseMs = 0;

// Set by the hook poll, cleared by the loop once reported.
//
// The default is a lifted receiver, and deliberately so. A hook that is not
// wired, or wired to the contact group that closes the other way round, reads
// HIGH — which the firmware would otherwise take for a receiver resting on
// its cradle, and a receiver resting on its cradle discards every digit and
// plays no sound. Starting from "lifted" means a broken hook costs a stale
// state that the first real transition corrects, rather than a telephone that
// is silent and gives no reason for it.
static volatile bool hookChanged = false;
static volatile bool hookLifted = true;

// Debounce state, one per contact. An edge this soon after the last
// accepted one is contact bounce.
static volatile uint32_t lastPulseEdgeMs = 0;
static volatile uint32_t lastHookEdgeMs = 0;

// The gap before each pulse of the train being counted, so a digit can be
// reported with the timing it was read from.
//
// This is what tells a real digit from a misread one. A train of a digit
// looks like 0, 100, 100, 100 — the first pulse arrives after the pause and
// the rest follow at the dial's own rate. Gaps in the hundreds of
// milliseconds between every pulse mean the pulses are not a train at all,
// and the usual cause is the wrong contact: the shunt contact (РИ) closes
// once per turn of the disc, so every digit reads as 1.
#define GAP_LOG 12
static volatile uint16_t pulseGaps[GAP_LOG];
static volatile uint8_t pulseGapCount = 0;

// ── the impulse contact ────────────────────────────────────────────────

// The level the pin was last seen at, and when it last changed. A level that
// has held DEBOUNCE_PULSE_MS without changing is settled; anything faster is
// the contact's make-and-break burst.
static bool pulseRaw = true;
static bool pulseSettled = true;
static uint32_t pulseChangedMs = 0;

// Polled from the loop. Counting the release (the pin rising back to the
// pull-up) rather than the closure makes the count the pulse count directly,
// and matches what the dial does: the contact closes once per unit as the
// disc returns, and it is the reopening that ends each unit.
static void readPulse() {
    const uint32_t now = millis();
    const bool level = (digitalRead(PIN_PULSE) == HIGH);

    // Any change restarts the settling clock, so a burst never settles.
    if (level != pulseRaw) {
        pulseRaw = level;
        pulseChangedMs = now;
        return;
    }

    if (now - pulseChangedMs < DEBOUNCE_PULSE_MS) {
        return;
    }

    if (level == pulseSettled) {
        return;
    }
    pulseSettled = level;

    // Only one of the two settled transitions is a pulse.
    if (!level) {
        return;
    }

    // Recorded before pulseCount changes, so the gap belongs to the pulse
    // being counted. The first pulse of a train has no meaningful gap — what
    // precedes it is the pause between digits — and is logged as 0.
    if (pulseGapCount < GAP_LOG) {
        pulseGaps[pulseGapCount++] =
            (pulseCount == 0) ? 0 : (uint16_t)(now - lastPulseMs);
    }

    pulseCount++;
    lastPulseMs = now;
}

// The hook switch, polled from the loop exactly as the impulse contact is.
//
// It used to be read inside an edge interrupt, which is what stopped it
// working. An interrupt fires on the first edge of the bounce burst and reads
// the pin there — in the middle of the contact making and breaking — so the
// level it samples is a coin toss. That alone would only misreport an event,
// but the handler also returns without doing anything when the level it read
// equals the state it already held. Sample the burst wrongly once and the
// firmware's idea of the hook is stuck inverted against the real receiver
// for good: every later edge then reads "no change" and is discarded, so the
// pin goes on switching cleanly while not one event is ever sent.
//
// That is the failure exactly: GPIO26 measured six clean transitions with no
// bounce at all, and the firmware reported none of them.
//
// Polling for a settled level cannot desynchronise. The level is compared
// against the last *settled* level rather than against the reported state,
// so even a missed report leaves the next transition detectable.
static bool hookRaw = true;
static bool hookSettled = true;
static uint32_t hookChangedMs = 0;

static void readHook() {
    if (PIN_HOOK < 0) {
        return;
    }

    const uint32_t now = millis();
    // The hook contact on this set is normally closed: it is made while the
    // receiver rests on the cradle and breaks when the receiver is lifted.
    // Closed pulls the pin to GND, so LOW is a receiver down and HIGH is a
    // receiver up — the opposite way round from a normally-open group.
    //
    // Measured on the bench: lifting the receiver printed "ПОЛОЖЕНА" and
    // replacing it printed "СНЯТА", every time. That inversion is also what
    // made the sound arrive at the wrong moment — the off-hook the server
    // waits for was sent as the receiver went down, so playback began into a
    // handset already back on its cradle.
    const bool level = (digitalRead(PIN_HOOK) == HIGH);

    // Any change restarts the settling clock, so a burst never settles.
    if (level != hookRaw) {
        hookRaw = level;
        hookChangedMs = now;
        return;
    }

    if (now - hookChangedMs < DEBOUNCE_HOOK_MS) {
        return;
    }

    if (level == hookSettled) {
        return;
    }
    hookSettled = level;

    hookLifted = level;
    hookChanged = true;
}

// ── the outbound queue ─────────────────────────────────────────────────
//
// The sender task owns the network; nothing else here blocks.

static QueueHandle_t events = nullptr;

// Whether an event says where the receiver is, rather than what was dialled.
static bool isHookEvent(const char *kind) {
    return strcmp(kind, "off-hook") == 0 || strcmp(kind, "on-hook") == 0;
}

// Queues an event, or drops it if the queue is full. Dropping is the right
// failure: the alternative is blocking the loop, and a loop that blocks
// misses pulses, which corrupts the digit being dialled rather than merely
// losing a report of it.
//
// A hook event does not queue behind the others. The receiver going down is
// what stops the music, and the queue ahead of it may hold digits and an
// older hook event that each burn three attempts against a four-second
// timeout before it is even tried — long enough for a sound to keep playing
// into a telephone that is back on its cradle.
//
// It is also the one event whose older copies are worthless. A digit is
// history and every one of them matters; the hook is a state, so the newest
// reading is the only true one and anything queued before it is describing a
// receiver position that has already been superseded. So a hook event clears
// the hook events waiting ahead of it and goes to the front.
static void report(const char *kind, const char *detail) {
    Event event;
    snprintf(event.kind, sizeof(event.kind), "%s", kind);
    snprintf(event.detail, sizeof(event.detail), "%s", detail ? detail : "");

    // Unconfigured, there is no sender task to drain the queue, so queueing
    // would fill it and then report every event as dropped. The console is
    // the whole output in that case.
    if (strlen(WIFI_SSID) == 0 || strlen(SERVER_HOST) == 0) {
        Serial.printf("[not sent] %s %s\n", event.kind, event.detail);
        return;
    }

    if (isHookEvent(kind)) {
        // Drain the stale hook events, keeping the digits in their order.
        // Everything is put back as it came out, minus the superseded hook
        // readings, so a number half-dialled before the receiver moved is
        // still reported in full.
        const UBaseType_t waiting = uxQueueMessagesWaiting(events);
        Event queued;
        for (UBaseType_t i = 0; i < waiting; i++) {
            if (xQueueReceive(events, &queued, 0) != pdTRUE) {
                break;
            }
            if (isHookEvent(queued.kind)) {
                continue;
            }
            xQueueSend(events, &queued, 0);
        }

        // To the front: the receiver's position is what the server needs
        // before anything else it might still be holding.
        if (xQueueSendToFront(events, &event, 0) != pdTRUE) {
            Serial.printf("queue full, dropped %s %s\n", event.kind, event.detail);
        }
        return;
    }

    if (xQueueSend(events, &event, 0) != pdTRUE) {
        Serial.printf("queue full, dropped %s %s\n", event.kind, event.detail);
    }
}

// ── the sender ─────────────────────────────────────────────────────────

// The status the server answered with, or 0 if the request never got that
// far. The distinction is what tells a retry worth making from one that will
// be refused identically every time.
static int post(const Event &event) {
    if (WiFi.status() != WL_CONNECTED) {
        return 0;
    }

    char url[96];
    snprintf(url, sizeof(url), "http://%s:%d%s", SERVER_HOST, SERVER_PORT,
             SERVER_PATH);

    char body[128];
    snprintf(body, sizeof(body),
             "{\"extension\":\"%s\",\"kind\":\"%s\",\"detail\":\"%s\"}",
             EXTENSION, event.kind, event.detail);

    HTTPClient http;
    if (!http.begin(url)) {
        return 0;
    }
    http.setTimeout(HTTP_TIMEOUT_MS);
    http.addHeader("Content-Type", "application/json");
    if (strlen(DIALER_TOKEN) > 0) {
        http.addHeader("X-Dialer-Token", DIALER_TOKEN);
    }

    const int status = http.POST((uint8_t *)body, strlen(body));
    http.end();

    if (status < 200 || status >= 300) {
        Serial.printf("POST %s %s -> %d\n", event.kind, event.detail, status);
    }
    return status;
}

static void senderTask(void *) {
    Event event;
    for (;;) {
        if (xQueueReceive(events, &event, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        for (int attempt = 0; attempt <= HTTP_RETRIES; attempt++) {
            const int status = post(event);

            if (status >= 200 && status < 300) {
                break;
            }

            // A 4xx is the server saying the request itself is wrong — an
            // unprogrammed number, a rejected token. Sending it again gets
            // the same answer, so retrying only fills the log three times
            // over. Only a failure that might pass later is worth repeating.
            if (status >= 400 && status < 500) {
                break;
            }

            // A newer hook reading arrived while this was being retried, so
            // what is in hand is already wrong about where the receiver is.
            // Retrying it would spend seconds delivering a stale position
            // and, worse, land it after the newer one — leaving the server
            // believing a replaced receiver is still lifted, with the music
            // playing on.
            //
            // report() puts a hook event at the front, so if one has arrived
            // it is the head of the queue and peeking finds it there. Only a
            // hook event supersedes this one; a digit queued behind it is
            // ordinary traffic and says nothing about the receiver.
            if (isHookEvent(event.kind)) {
                Event next;
                if (xQueuePeek(events, &next, 0) == pdTRUE && isHookEvent(next.kind)) {
                    Serial.printf("superseded, dropped %s\n", event.kind);
                    break;
                }
            }

            // Long enough to outlast a brief reconnection, short enough
            // that a queued hangup still arrives promptly.
            vTaskDelay(pdMS_TO_TICKS(200));
        }
    }
}

// ── Wi-Fi ──────────────────────────────────────────────────────────────
//
// Kept in a task as well, so a dropped link reconnects without the loop
// waiting on it.

static void wifiTask(void *) {
    WiFi.mode(WIFI_STA);
    // The ESP32 sleeps its radio between beacons by default, which adds
    // enough latency to a POST to be noticeable on a hangup.
    WiFi.setSleep(false);

    for (;;) {
        if (WiFi.status() != WL_CONNECTED) {
            Serial.printf("connecting to %s\n", WIFI_SSID);
            WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
            // Give the association a chance before judging it failed.
            for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) {
                vTaskDelay(pdMS_TO_TICKS(250));
            }
            if (WiFi.status() == WL_CONNECTED) {
                Serial.print("connected, address ");
                Serial.println(WiFi.localIP());
            } else {
                WiFi.disconnect();
                vTaskDelay(pdMS_TO_TICKS(2000));
            }
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

// ── the number being dialled ───────────────────────────────────────────

static char number[NUMBER_LENGTH + 1] = {0};
static uint8_t digits = 0;
static uint32_t lastDigitMs = 0;

static void clearNumber() {
    number[0] = '\0';
    digits = 0;
}

// One completed pulse train, turned into the digit it stands for.
//
// The gaps are carried in only to be printed. A digit that reads wrong is
// almost never wrong arithmetic — it is pulses that never arrived — and the
// gaps are the only thing that says which.
static void onDigit(uint32_t pulses, const uint16_t *gaps, uint8_t gapCount) {
    // A train that cannot be a digit is said to be one, with its count, so a
    // miscount is visible rather than silently absent from the console.
    if (pulses == 0 || pulses > MAX_PULSES) {
        Serial.printf("ЦИФРА: — (отброшено, импульсов %u)\n", pulses);
        return;
    }

    // Ten pulses is 0; everything else is its own count.
    const char digit = (pulses == 10) ? '0' : ('0' + (char)pulses);

    // The digit first and on a line of its own: it is what the caller
    // dialled, and everything under it is only the working behind it.
    Serial.printf("ЦИФРА: %c   (импульсов %u)\n", digit, pulses);

    // The timing of what was just counted, printed for every digit: it costs
    // one line and it is the difference between "the digit is wrong" and
    // knowing why.
    Serial.printf("   (паузы:");
    for (uint8_t i = 0; i < gapCount; i++) {
        Serial.printf(" %u", gaps[i]);
    }
    Serial.println(" мс)");

    // A single pulse is what the wrong contact produces. The shunt contact
    // (РИ) closes once per turn of the disc whatever digit was dialled, so
    // every digit arrives as 1 — which is the symptom worth naming, because
    // it looks like a firmware fault and is a wiring one.
    if (pulses == 1) {
        Serial.println("   один импульс. Если каждая цифра читается как 1,");
        Serial.println("   провод, скорее всего, на шунтирующем контакте (РИ),");
        Serial.println("   а не на импульсном (НИ). Наберите 5 с прозвонкой:");
        Serial.println("   НИ щёлкает пять раз, РИ — один.");
    }

    // A part-dialled number left sitting is abandoned, not the start of
    // this one.
    const uint32_t now = millis();
    if (digits > 0 && now - lastDigitMs > NUMBER_TIMEOUT_MS) {
        Serial.println("   (сброшен незаконченный набор — слишком долгая пауза)");
        clearNumber();
    }
    lastDigitMs = now;

    const char text[2] = {digit, '\0'};
    report("digit", text);

    if (digits < NUMBER_LENGTH) {
        number[digits++] = digit;
        number[digits] = '\0';
    }

    if (digits == NUMBER_LENGTH) {
        Serial.printf("НОМЕР НАБРАН: %s\n", number);
        report("number", number);
        clearNumber();
    }
}

// ── setup and loop ─────────────────────────────────────────────────────

void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.println("\nTA-1132 dial reader");

    // Built without config.local.h, which ./flash.sh writes. The dial still
    // reads and the console still shows every pulse, so the wiring can be
    // checked; nothing is reported anywhere.
    if (strlen(WIFI_SSID) == 0 || strlen(SERVER_HOST) == 0) {
        Serial.println("no network configured — run ./flash.sh to set one.");
        Serial.println("the dial is still read, and shown here only.");
    } else {
        Serial.printf("reporting %s to %s:%d as extension %s\n",
                      WIFI_SSID, SERVER_HOST, SERVER_PORT, EXTENSION);
    }

    pinMode(PIN_PULSE, INPUT_PULLUP);
    if (PIN_HOOK >= 0) {
        pinMode(PIN_HOOK, INPUT_PULLUP);
    }

    // The pulse pin's settled state starts from what the pin actually reads,
    // so a contact resting closed at power-on is not mistaken for a pulse the
    // first time it opens.
    pulseRaw = pulseSettled = (digitalRead(PIN_PULSE) == HIGH);
    pulseChangedMs = millis();

    events = xQueueCreate(EVENT_QUEUE_DEPTH, sizeof(Event));

    // With nothing to connect to, the two network tasks would spend the whole
    // run failing to associate and retrying. The queue still exists and
    // report() still fills it; only the sending is left out.
    if (strlen(WIFI_SSID) > 0 && strlen(SERVER_HOST) > 0) {
        xTaskCreatePinnedToCore(wifiTask, "wifi", 4096, nullptr, 1, nullptr, 0);
        xTaskCreatePinnedToCore(senderTask, "sender", 8192, nullptr, 1, nullptr, 0);
    }

    // The receiver starts lifted, whatever the pin says and whether or not a
    // hook is wired at all. It is the state that lets everything work: digits
    // count, and a sound armed for this handset plays the moment it is asked
    // for. The opposite default is the one that fails silently — a hook that
    // is unwired, miswired, or simply resting open reads as a receiver down,
    // and a receiver down throws away every digit dialled and refuses to play
    // anything, with nothing on the console to say why.
    //
    // Nothing is lost by starting here. The settled level below still comes
    // from the pin, so the first genuine transition — the receiver actually
    // being put down — is detected and reported exactly as before; only the
    // starting assumption differs, and a wrong one costs one stale reading
    // that the next movement of the hook corrects.
    hookLifted = true;

    if (PIN_HOOK >= 0) {
        // Settled state from the pin, so a real transition is what triggers
        // the next report rather than the gap between the assumption and the
        // reading. Started at "lifted" rather than at the pin's level: with a
        // receiver genuinely resting at power-on, the pin reads HIGH and the
        // first poll then reports on-hook, which tells the server the true
        // position within a debounce window instead of leaving it believing
        // the optimistic default indefinitely.
        hookRaw = hookSettled = true;
        hookChangedMs = millis();

        const bool pinLifted = (digitalRead(PIN_HOOK) == HIGH);
        Serial.printf("ТРУБКА: СНЯТА (по умолчанию; пин читается %s)\n",
                      pinLifted ? "HIGH/разомкнут — трубка действительно снята"
                                : "LOW/замкнут — уточнится при опросе");
    } else {
        Serial.println("ТРУБКА: СНЯТА (датчик не подключён, PIN_HOOK -1)");
    }

    // The server is told at boot, so it does not have to wait for the first
    // movement of the hook to learn where the receiver is. A handset that came
    // up while the firmware was resetting would otherwise never be reported at
    // all — the transition happened while nothing was polling.
    report("off-hook", "");
}

void loop() {
    // Both contacts are polled rather than interrupted: an edge only counts
    // once the level has settled, and settling is a thing the loop can see but
    // an edge interrupt cannot.
    readPulse();
    readHook();

    // The hook next: putting the receiver down ends whatever was being
    // dialled, so handling it before the digit keeps a half-dialled number
    // from completing after the call is over.
    //
    // Nothing here masks interrupts any more. Both contacts are read by
    // readPulse() and readHook(), which run in this loop, so no handler can
    // land between two of these statements and there is nothing to guard
    // against.
    if (hookChanged) {
        hookChanged = false;
        const bool lifted = hookLifted;

        if (lifted) {
            Serial.println("ТРУБКА: СНЯТА");
            clearNumber();
            // A train counted before the receiver came up belongs to no
            // call and would otherwise land on this one.
            pulseCount = 0;
            pulseGapCount = 0;
            report("off-hook", "");
        } else {
            Serial.println("ТРУБКА: ПОЛОЖЕНА");
            clearNumber();
            pulseCount = 0;
            pulseGapCount = 0;
            report("on-hook", "");
        }
    }

    // A train that has gone quiet long enough is a finished digit. Taking
    // the count and clearing it together keeps the gaps and the count
    // describing the same train. Both are written by readPulse(), which runs
    // in this loop rather than an interrupt, so nothing can land between the
    // two statements.
    uint16_t gaps[GAP_LOG];
    uint8_t gapCount = 0;

    const uint32_t pulses = pulseCount;
    const uint32_t since = millis() - lastPulseMs;
    bool complete = false;
    if (pulses > 0 && since >= DIGIT_GAP_MS) {
        gapCount = pulseGapCount;
        for (uint8_t i = 0; i < gapCount; i++) {
            gaps[i] = pulseGaps[i];
        }
        pulseGapCount = 0;
        pulseCount = 0;
        complete = true;
    }

    if (complete) {
        // Dialling with the receiver down drives the contacts but places no
        // call, and reporting it would open one at the server.
        if (hookLifted) {
            onDigit(pulses, gaps, gapCount);
        } else {
            // Naming the raw level as well: a hook that never reads as
            // lifted is either not wired to the pin or wired to the contact
            // group that closes the other way round, and the level says
            // which. LOW here with the receiver up means the contact is
            // closed when it should be open — a normally-open group, or a
            // wire shorted to ground.
            Serial.printf("ЦИФРА: — (набор при положенной трубке, пропущено;"
                          " пин крючка читается %s)\n",
                          digitalRead(PIN_HOOK) == HIGH ? "HIGH/разомкнут"
                                                        : "LOW/замкнут");
        }
    }

    // Short enough that the settling window is sampled several times over:
    // at 15 ms a 5 ms loop could see a level change land just after one pass
    // and be judged settled on the next but one, which shortens the window it
    // is meant to enforce.
    delay(1);
}
