/*
  Buckshot Roulette IRL — физический триггер соленоида (ESP32).

  Архитектура (максимально быстрая реакция на курок):
    - RF-сигнал 433МГц декодируется ПРЯМО В ПРЕРЫВАНИИ (ISR), по фронтам,
      «на лету»: каждый бит кадра проверяется в момент приёма его импульсов.
      На последнем бите кадра код сравнивается с KNOWN_TRIGGER_CODE, и если
      совпал — соленоид включается ТУТ ЖЕ, в том же прерывании, прямой записью
      в GPIO-регистр. Между приёмом последнего бита и ударом соленоида —
      единицы микросекунд. Никакого опроса из loop(), никакого влияния
      Wi-Fi/HTTP на задержку. Выбор цели оператором («в кого попали») идёт
      ПАРАЛЛЕЛЬНО через сервер и на удар соленоида не влияет — сначала выстрел,
      меню появляется после.
    - Достаточно ОДНОГО валидного кадра (раньше RCSwitch требовал 2 повтора
      внутри себя + прошивка ждала ещё 2 подтверждения = 4 кадра ≈ 180мс).
      Защита от мусора — не количеством пакетов, а качеством проверки:
      кадр обязан пройти повременную валидацию КАЖДОГО импульса (допуск 60%
      от юнита протокола) и дать точное совпадение всех 24 бит кода.
      Вероятность, что эфирный шум случайно соберёт такой кадр, исчезающе
      мала (< 2^-24 на каждую попытку, и попытка требует ещё и валидных
      таймингов всех 48 импульсов).
    - trigger_remote.pulse_us в config.json (печатается в режиме обучения):
      если задан, декодер знает длительность юнита заранее и может распознать
      САМЫЙ ПЕРВЫЙ кадр пачки — реакция ≈ длительность одного кадра (~35мс
      для протокола 1, это физический минимум самого радиопротокола).
      Если 0 — юнит вычисляется из sync-паузы, срабатывание со 2-го кадра.
    - Выключение соленоида, лог и уведомление сервера делает отдельная
      FreeRTOS-задача с высоким приоритетом (просыпается по notify из ISR),
      поэтому импульс соленоида не зависит от блокирующего HTTP в loop().
    - «Глухое» окно RF_LOCKOUT_MS после срабатывания поглощает хвост пачки
      (пульт шлёт один код многократно): одно нажатие = ровно один выстрел.
    - Сервер остаётся источником истины: решение боевой/холостой принимается
      по кэшу (cachedReady/cachedLive), который loop() обновляет фоновым
      опросом GET /api/esp/shell_status раз в POLL_INTERVAL_MS.
    - Пока кэш говорит «следующий патрон боевой», лампочка LIVE_LED_PIN мигает.
    - Режим обучения (KNOWN_TRIGGER_CODE == 0): работает классический RCSwitch,
      печатает код/протокол/битность/длительность юнита пойманного пульта —
      эти значения вписываются в config.json (включая pulse_us).

  Требования ТЗ:
    - Safety timeout: соленоид не может зависнуть в HIGH дольше
      SOLENOID_MAX_ON_MS (выключает и задача, и подстраховка в loop()).
    - Wi-Fi: автопереподключение, не блокирующее loop().
    - Watchdog: если loop() завис — чип перезагружается сам.
*/

#include <WiFi.h>
#include <HTTPClient.h>
#include <RCSwitch.h>
#include <esp_task_wdt.h>
#include <soc/gpio_struct.h>

// Все настройки (Wi-Fi, адрес сервера, пины, код пульта-курка) берутся из
// config.h, который генерируется из корневого config.json скриптом
// esp/gen_config.py. Перед прошивкой выполни:  python esp/gen_config.py
//
// Чтобы узнать параметры нового пульта: поставь "code": 0 в config.json,
// перегенерируй config.h, прошей — режим обучения напечатает код, протокол,
// битность и длительность юнита (pulse_us). Впиши их и перепрошей.
#include "config.h"

// Старый config.h мог быть сгенерирован без этого поля — не падаем на сборке.
#ifndef CFG_TRIGGER_PULSE_US
#define CFG_TRIGGER_PULSE_US 0UL
#endif

RCSwitch rfTrigger = RCSwitch();  // используется ТОЛЬКО в режиме обучения

// ── Тайминги и пороги ──
// Значения приходят из config.json (блок esp.timings) через config.h.
static const unsigned long POLL_INTERVAL_MS = CFG_POLL_INTERVAL_MS;      // фоновый опрос статуса патрона
static const unsigned long HTTP_TIMEOUT_MS = CFG_HTTP_TIMEOUT_MS;        // таймаут HTTP-запросов
static const unsigned long SOLENOID_PULSE_MS = CFG_SOLENOID_PULSE_MS;    // длительность импульса на соленоид
static const unsigned long SOLENOID_MAX_ON_MS = CFG_SOLENOID_MAX_ON_MS;  // аварийный предел удержания
// «Глухой» период после срабатывания: пульт шлёт код пачкой одинаковых кадров,
// всё что декодировалось в это окно — хвост того же нажатия, игнорируем.
static const unsigned long RF_LOCKOUT_MS = CFG_RF_LOCKOUT_MS;
static const unsigned long WIFI_RETRY_INTERVAL_MS = CFG_WIFI_RETRY_INTERVAL_MS;
static const unsigned long LIVE_LED_BLINK_MS = CFG_LIVE_LED_BLINK_MS;
static const uint32_t WDT_TIMEOUT_MS = CFG_WDT_TIMEOUT_MS;
// Длительность юнита пульта в мкс (0 = вычислять из sync-паузы, см. шапку).
static const uint32_t RF_PULSE_US = CFG_TRIGGER_PULSE_US;

// ── Таблица протоколов 433МГц (тайминги из библиотеки RCSwitch) ──
// Все длительности — в юнитах pulseUs. Кадр: [биты][sync], sync задаёт паузу
// между кадрами, по которой (при pulse_us == 0) вычисляется юнит.
struct RfProto {
    uint16_t pulseUs;                 // номинальный юнит (справочно)
    uint8_t syncHi, syncLo;           // sync: HIGH/LOW в юнитах
    uint8_t zeroHi, zeroLo;           // бит «0»
    uint8_t oneHi, oneLo;             // бит «1»
    bool inverted;
};
static const RfProto RF_PROTOS[] = {
    {350, 1, 31, 1, 3, 3, 1, false},     // 1
    {650, 1, 10, 1, 2, 2, 1, false},     // 2
    {100, 30, 71, 4, 11, 9, 6, false},   // 3
    {380, 1, 6, 1, 3, 3, 1, false},      // 4
    {500, 6, 14, 1, 2, 2, 1, false},     // 5
    {450, 23, 1, 1, 2, 2, 1, true},      // 6  (HT6P20B)
    {150, 2, 62, 1, 6, 6, 1, false},     // 7  (HS2303-PT)
    {200, 3, 130, 7, 16, 3, 16, false},  // 8  (Conrad RS-200 RX)
    {200, 130, 7, 16, 7, 16, 3, true},   // 9  (Conrad RS-200 TX)
    {365, 18, 1, 3, 1, 1, 3, true},      // 10 (1ByOne Doorbell)
    {270, 36, 1, 1, 2, 2, 1, true},      // 11 (HT12E)
    {320, 36, 1, 1, 2, 2, 1, true},      // 12 (SM5212)
};
static const int RF_PROTO_COUNT = sizeof(RF_PROTOS) / sizeof(RF_PROTOS[0]);

// Пауза длиннее этого — граница кадра/пачки (та же константа, что в RCSwitch).
static const uint32_t RF_SEPARATION_US = 4300;

// Соленоидом из ISR управляем прямой записью в регистры GPIO.out_w1ts/w1tc —
// это атомарно, ISR-безопасно и занимает наносекунды. Регистры покрывают
// только GPIO 0-31.
static_assert(SOLENOID_PIN < 32, "SOLENOID_PIN должен быть GPIO 0-31");
static const uint32_t SOLENOID_BIT = (1UL << SOLENOID_PIN);

// ── Состояние (loop-контекст) ──
volatile bool cachedReady = false;  // volatile: читается из ISR при выстреле
volatile bool cachedLive = false;
// Блокировка соленоида до выбора цели оператором. Ставится В ISR сразу при
// физическом выстреле (мгновенно, без ожидания сервера — иначе между выстрелом
// и обновлением кэша можно успеть нажать курок ещё раз). Снимается в loop(),
// когда опрос статуса увидит, что сервер закрыл ожидание (pending_shot=false),
// т.е. дилер выбрал, в кого попали. Пока стоит — курок соленоид не дёргает.
volatile bool awaitingTarget = false;
volatile uint32_t awaitingSinceUs = 0;  // когда встала блокировка (для предохранителя)
// Опрос статуса уже подтвердил, что сервер увидел выстрел (pending поднялся).
// Только после этого переход pending→false означает выбор цели, а не зазор
// между выстрелом и подъёмом флага на сервере. Читается/пишется только в loop().
bool pendingSeen = false;
// Предохранитель: если дилер так и не выбрал цель (или POST /shoot не дошёл до
// сервера — сеть моргнула), блокировка не должна залипнуть навсегда. Через этот
// таймаут снимаем её принудительно, чтобы курок снова работал.
static const uint32_t AWAIT_TARGET_TIMEOUT_MS = 30000;
unsigned long lastPollMs = 0;
unsigned long lastWifiAttemptMs = 0;

volatile bool solenoidOn = false;
volatile uint32_t solenoidOnSinceUs = 0;

bool liveLedOn = false;
unsigned long lastLiveLedToggleMs = 0;

// Флаг «нужно сообщить серверу о выстреле». Ставится задачей триггера,
// HTTP-запрос уходит из loop() — сетевые вызовы не трогают горячий путь.
volatile bool pendingShoot = false;

bool fastRfActive = false;  // true = быстрый ISR-декодер, false = обучение

// ── Параметры выбранного протокола (заполняются в setup до attachInterrupt) ──
static uint8_t rfZeroHi, rfZeroLo, rfOneHi, rfOneLo;
static uint8_t rfSyncDiv;   // делитель sync-паузы для вычисления юнита
static uint8_t rfSkipInit;  // у инвертированных протоколов данные на 1 фронт позже

// ── Состояние декодера (только из ISR) ──
static const uint8_t RF_IDLE = 0xFF;        // «кадр не идёт, ждём sync-паузу»
static uint32_t rfLastEdgeUs = 0;
static uint8_t rfBitCount = RF_IDLE;
static uint8_t rfSkip = 0;
static bool rfHaveFirst = false;
static uint32_t rfFirstDur = 0;
static uint32_t rfCode = 0;
static uint32_t rfZeroHiUs, rfZeroLoUs, rfOneHiUs, rfOneLoUs, rfTolUs;
static uint32_t rfFrameStartUs = 0;
// Инициализация «в прошлом», чтобы lockout не глушил первое нажатие после старта.
static volatile uint32_t lastFireUs = (uint32_t)(0UL - CFG_RF_LOCKOUT_MS * 1000UL);
// Момент последнего валидного кадра ЛЮБОЙ пачки (продлевается на каждом кадре).
// По паузе относительно него отличаем новое нажатие от хвоста той же пачки.
static volatile uint32_t lastSeenUs = (uint32_t)(0UL - CFG_RF_LOCKOUT_MS * 1000UL);

// ── Событие выстрела: ISR → задача триггера ──
static TaskHandle_t triggerTaskHandle = NULL;
static volatile bool fireIsLive = false;      // боевой (соленоид уже включён в ISR)
static volatile bool fireAdvance = false;     // просить сервер продвинуть патрон
static volatile uint32_t fireDecodedUs = 0;   // момент декодирования последнего бита
static volatile uint32_t fireFrameStartUs = 0;

static inline uint32_t IRAM_ATTR udiff(uint32_t a, uint32_t b) {
    return a > b ? a - b : b - a;
}

// Решение по декодированному валидному коду. Вызывается из ISR на последнем
// бите кадра — здесь каждая инструкция на счету, никакого Serial/HTTP.
static void IRAM_ATTR rfFire(uint32_t nowUs) {
    // Пульт при ОДНОМ нажатии шлёт код пачкой одинаковых кадров. Нельзя
    // отсчитывать lockout от МОМЕНТА ПЕРВОГО кадра фиксированным окном: если
    // пачка длится дольше RF_LOCKOUT_MS (палец удерживают / длинная посылка),
    // кадр за окном пройдёт как «новое нажатие» → второй удар соленоида.
    //
    // Правильно: считать нажатия РАЗДЕЛЁННЫМИ ПАУЗОЙ в эфире. lastSeenUs
    // обновляем на КАЖДОМ валидном кадре (продлеваем «занятость» эфира), а
    // новый выстрел разрешаем только когда с последнего виденного кадра прошёл
    // весь RF_LOCKOUT_MS — то есть пачка реально закончилась, палец отпущен.
    uint32_t sinceSeen = nowUs - lastSeenUs;
    lastSeenUs = nowUs;
    if (sinceSeen < RF_LOCKOUT_MS * 1000UL) {
        return;  // тот же непрерывный поток кадров — хвост одного нажатия
    }

    // Предыдущий выстрел ещё ждёт, пока дилер выберет цель — соленоид заблокирован.
    // Игнорируем курок целиком: ни удара, ни новой регистрации. Разблокирует
    // loop(), когда сервер закроет ожидание (см. awaitingTarget / pollShellStatus).
    if (awaitingTarget) {
        return;
    }

    lastFireUs = nowUs;

    fireIsLive = cachedReady && cachedLive;
    fireAdvance = cachedReady;  // курок спущен = патрон уходит (и боевой, и холостой)
    fireDecodedUs = nowUs;
    fireFrameStartUs = rfFrameStartUs;

    if (fireIsLive) {
        // Самый горячий путь: соленоид — НЕМЕДЛЕННО, прямой записью в регистр.
        // Между приёмом последнего бита курка и ударом — единицы микросекунд.
        GPIO.out_w1ts = SOLENOID_BIT;
        solenoidOn = true;
        solenoidOnSinceUs = nowUs;
    }

    // Блокируем соленоид до выбора цели. Ставим здесь же, в ISR, при ЛЮБОМ
    // валидном курке (боевой/холостой) — блокировка не зависит от того, ударил
    // ли соленоид: одно физическое нажатие = один цикл «выстрел → выбор цели».
    if (fireAdvance) {
        awaitingTarget = true;
        awaitingSinceUs = nowUs;
    }

    // Будим задачу триггера: она выдержит импульс, выключит соленоид, напечатает
    // лог и поставит pendingShoot. Выбор цели оператором идёт ПАРАЛЛЕЛЬНО и на
    // удар соленоида уже не влияет — выстрел мгновенный, меню появляется после.
    BaseType_t woken = pdFALSE;
    vTaskNotifyGiveFromISR(triggerTaskHandle, &woken);
    if (woken == pdTRUE) {
        portYIELD_FROM_ISR();
    }
}

// Потоковый декодер: вызывается на КАЖДОМ фронте сигнала с приёмника.
// Валидирует кадр импульс за импульсом; на последнем бите — сравнение кода и
// (при совпадении) немедленный выстрел. Любое несовпадение таймингов сразу
// сбрасывает декодер в ожидание следующей sync-паузы — шум отсеивается дёшево.
static void IRAM_ATTR rfIsr() {
    uint32_t nowUs = micros();
    uint32_t dur = nowUs - rfLastEdgeUs;
    rfLastEdgeUs = nowUs;

    if (dur > RF_SEPARATION_US) {
        // Длинная пауза — граница кадра. Начинаем приём нового кадра.
        uint32_t unit = RF_PULSE_US;
        if (unit == 0) {
            // Юнит не задан в конфиге — вычисляем из sync-паузы (как RCSwitch).
            // Паузы «не того» масштаба (тишина эфира, шум) отбрасываем.
            unit = dur / rfSyncDiv;
            if (unit < 50 || unit > 2000) {
                rfBitCount = RF_IDLE;
                return;
            }
        }
        rfZeroHiUs = unit * rfZeroHi;
        rfZeroLoUs = unit * rfZeroLo;
        rfOneHiUs = unit * rfOneHi;
        rfOneLoUs = unit * rfOneLo;
        rfTolUs = unit * 60 / 100;  // допуск 60% юнита — как в RCSwitch
        rfCode = 0;
        rfBitCount = 0;
        rfHaveFirst = false;
        rfSkip = rfSkipInit;
        rfFrameStartUs = nowUs;
        return;
    }

    if (rfBitCount == RF_IDLE) {
        return;  // кадр не идёт — ждём sync-паузу
    }
    if (rfSkip) {
        rfSkip--;
        return;
    }

    // Биты приходят парами длительностей (HIGH, LOW).
    if (!rfHaveFirst) {
        rfFirstDur = dur;
        rfHaveFirst = true;
        return;
    }
    rfHaveFirst = false;

    if (udiff(rfFirstDur, rfZeroHiUs) < rfTolUs && udiff(dur, rfZeroLoUs) < rfTolUs) {
        rfCode <<= 1;  // бит «0»
    } else if (udiff(rfFirstDur, rfOneHiUs) < rfTolUs && udiff(dur, rfOneLoUs) < rfTolUs) {
        rfCode = (rfCode << 1) | 1;  // бит «1»
    } else {
        rfBitCount = RF_IDLE;  // тайминги не сошлись — это шум, ждём новый кадр
        return;
    }

    if (++rfBitCount >= KNOWN_TRIGGER_BITLENGTH) {
        rfBitCount = RF_IDLE;
        if (rfCode == KNOWN_TRIGGER_CODE) {
            rfFire(nowUs);
        }
    }
}

// Задача триггера: спит на notify из ISR. Соленоид к её пробуждению УЖЕ включён
// (боевой) — задача только выдерживает импульс, выключает, логирует и просит
// loop() уведомить сервер. Высокий приоритет + отдельное от loop() ядро:
// блокирующий HTTP-опрос никак не влияет ни на импульс, ни на его длительность.
void triggerTask(void *) {
    for (;;) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        uint32_t wakeUs = micros();
        bool live = fireIsLive;
        bool advance = fireAdvance;
        uint32_t decodedUs = fireDecodedUs;
        uint32_t frameStartUs = fireFrameStartUs;

        if (live) {
            // Печатаем, пока идёт удержание импульса — Serial ничего не задерживает.
            Serial.println(">>> Курок: боевой патрон — соленоид включён из ISR!");
            Serial.printf("[ЗАМЕР] Задержка курок→соленоид: %.1f мс (приём кадра), ISR→задача: %lu мкс\n",
                          (decodedUs - frameStartUs) / 1000.0,
                          (unsigned long)(wakeUs - decodedUs));
            uint32_t elapsedMs = (micros() - decodedUs) / 1000;
            if (elapsedMs < SOLENOID_PULSE_MS) {
                vTaskDelay(pdMS_TO_TICKS(SOLENOID_PULSE_MS - elapsedMs));
            }
            GPIO.out_w1tc = SOLENOID_BIT;
            solenoidOn = false;
        } else {
            Serial.println(">>> Курок: холостой (или сервер недоступен/не фаза стрельбы) — соленоид молчит.");
        }

        // Выбор цели оператором идёт ПАРАЛЛЕЛЬНО и на удар уже не влияет.
        if (advance) {
            pendingShoot = true;  // HTTP уйдёт из loop()
        }
    }
}

void updateLiveLed() {
    // Мигание синего диода перед боевым патроном ОТКЛЮЧЕНО: оно раскрывало
    // игрокам тип следующего патрона (спойлер). Диод держим погашенным.
    if (liveLedOn) {
        liveLedOn = false;
        digitalWrite(LIVE_LED_PIN, LOW);
    }
}

// Управление соленоидом из loop-контекста (принудительный импульс дилера,
// аварийное выключение). Горячий путь выстрела идёт мимо — прямо в ISR.
void setSolenoid(bool on) {
    solenoidOn = on;
    digitalWrite(SOLENOID_PIN, on ? HIGH : LOW);
    if (on) {
        solenoidOnSinceUs = micros();
    }
}

const char* wifiStatusToStr(wl_status_t status) {
    switch (status) {
        case WL_IDLE_STATUS:     return "IDLE";
        case WL_NO_SSID_AVAIL:   return "NO_SSID_AVAIL (сеть с таким именем не видна)";
        case WL_SCAN_COMPLETED:  return "SCAN_COMPLETED";
        case WL_CONNECTED:       return "CONNECTED";
        case WL_CONNECT_FAILED:  return "CONNECT_FAILED (неверный пароль или тип защиты)";
        case WL_CONNECTION_LOST: return "CONNECTION_LOST";
        case WL_DISCONNECTED:    return "DISCONNECTED";
        default:                 return "UNKNOWN";
    }
}

void scanNetworks() {
    Serial.println("[WiFi] Сканирую доступные сети...");
    int n = WiFi.scanNetworks();
    if (n <= 0) {
        Serial.println("[WiFi] Сети не найдены.");
        return;
    }
    for (int i = 0; i < n; i++) {
        Serial.print("  - \"");
        Serial.print(WiFi.SSID(i));
        Serial.print("\" RSSI=");
        Serial.print(WiFi.RSSI(i));
        Serial.print(" enc=");
        Serial.println(WiFi.encryptionType(i));
    }
    WiFi.scanDelete();
}

void ensureWifiConnected() {
    static bool wasConnected = false;

    if (WiFi.status() == WL_CONNECTED) {
        if (!wasConnected) {
            wasConnected = true;
            Serial.print("[WiFi] Подключено! IP: ");
            Serial.println(WiFi.localIP());
        }
        return;
    }

    if (wasConnected) {
        wasConnected = false;
        Serial.println("[WiFi] Соединение потеряно.");
    }

    unsigned long now = millis();
    if (now - lastWifiAttemptMs < WIFI_RETRY_INTERVAL_MS) {
        return;
    }
    lastWifiAttemptMs = now;
    Serial.print("[WiFi] Не подключено (статус: ");
    Serial.print(wifiStatusToStr(WiFi.status()));
    Serial.println("), пробую переподключиться...");
    // ВАЖНО: не дёргаем WiFi.disconnect() — он рвёт ещё идущий WPA2-хендшейк
    // (тот занимает несколько секунд). Просто повторяем begin(); ESP сам
    // продолжит попытку. disconnect() тут был причиной вечного DISCONNECTED.
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

// Запрашивает у сервера статус следующего патрона и обновляет cachedReady/
// cachedLive. Возвращает true, если сервер ответил (данные свежие). Сбрасывает
// таймер фонового опроса — синхронный вызов при выстреле считается за опрос.
bool pollShellStatus() {
    lastPollMs = millis();

    if (WiFi.status() != WL_CONNECTED) {
        cachedReady = false;
        return false;
    }

    HTTPClient http;
    // setTimeout ограничивает только чтение ответа; TCP-connect к недоступному
    // хосту без setConnectTimeout висит дольше WDT_TIMEOUT_MS и валит плату
    // в перезагрузку по watchdog. Ограничиваем оба этапа.
    http.setConnectTimeout(HTTP_TIMEOUT_MS);
    http.setTimeout(HTTP_TIMEOUT_MS);
    String url = String(SERVER_BASE_URL) + "/api/esp/shell_status";
    if (!http.begin(url)) {
        cachedReady = false;
        return false;
    }

    int code = http.GET();
    bool ok = false;
    if (code == HTTP_CODE_OK) {
        String body = http.getString();
        // Крошечный JSON вида {"ready": true, "live": false} — парсим руками,
        // чтобы не тащить в проект библиотеку ради двух булевых полей.
        bool ready = body.indexOf("\"ready\": true") >= 0 || body.indexOf("\"ready\":true") >= 0;
        bool live = body.indexOf("\"live\": true") >= 0 || body.indexOf("\"live\":true") >= 0;
        bool pending = body.indexOf("\"pending\": true") >= 0 || body.indexOf("\"pending\":true") >= 0;
        // Порядок записи важен: ISR читает пару без блокировки. Сначала гасим
        // ready (ISR перестаёт стрелять), потом обновляем live, потом ready.
        cachedReady = false;
        cachedLive = live;
        cachedReady = ready;

        // Разблокировка соленоида после выбора цели дилером. awaitingTarget
        // ставит ISR мгновенно при выстреле, а pending_shot на сервере поднимется
        // лишь после POST /shoot из loop() — поэтому сначала ДОЖИДАЕМСЯ, что
        // сервер реально увидел выстрел (pending=true, флаг pendingSeen), и лишь
        // потом переход pending: true→false трактуем как «дилер выбрал цель».
        // Без pendingSeen опрос, попавший в зазор до подъёма pending, снял бы
        // блокировку преждевременно.
        if (awaitingTarget) {
            if (pending) {
                pendingSeen = true;
            } else if (pendingSeen) {
                awaitingTarget = false;
                pendingSeen = false;
                Serial.println("[КУРОК] Дилер выбрал цель — соленоид разблокирован.");
            }
        }

        // Команда дилера «принудительно щёлкнуть соленоидом» (кнопка в панели).
        // Сервер выставляет fire=true разово; отрабатываем импульс сразу, минуя
        // игровую логику боевого/холостого.
        bool fire = body.indexOf("\"fire\": true") >= 0 || body.indexOf("\"fire\":true") >= 0;
        if (fire) {
            Serial.println(">>> Дилер: принудительный импульс на соленоид!");
            setSolenoid(true);
            delay(SOLENOID_PULSE_MS);
            setSolenoid(false);
        }
        ok = true;
    } else {
        Serial.print("[HTTP] Ошибка опроса статуса патрона, код: ");
        Serial.println(code);
        cachedReady = false;
    }
    http.end();
    return ok;
}

// Сообщает серверу о выстреле: POST /api/esp/shoot выталкивает текущий патрон
// из очереди (патрон "продвигается"). После этого сразу обновляем кэш свежим
// статусом следующего патрона. Вызывается из loop(), не из горячего пути.
void sendShootToServer() {
    if (WiFi.status() != WL_CONNECTED) {
        return;
    }

    HTTPClient http;
    http.setConnectTimeout(HTTP_TIMEOUT_MS);
    http.setTimeout(HTTP_TIMEOUT_MS);
    String url = String(SERVER_BASE_URL) + "/api/esp/shoot";
    if (!http.begin(url)) {
        return;
    }
    http.addHeader("Content-Type", "application/x-www-form-urlencoded");
    int code = http.POST("");
    if (code == HTTP_CODE_OK) {
        Serial.println("[HTTP] Выстрел зарегистрирован, патрон продвинут.");
    } else {
        Serial.print("[HTTP] Ошибка регистрации выстрела, код: ");
        Serial.println(code);
    }
    http.end();

    // Сразу подтягиваем статус следующего патрона, чтобы LED/кэш обновились
    // не дожидаясь очередного фонового опроса.
    pollShellStatus();
}

// Режим обучения: печатает параметры пойманного пульта, чтобы вписать их в
// config.json (code, protocol, bitlength и pulse_us — длительность юнита).
void handleRfLearning() {
    if (!rfTrigger.available()) {
        return;
    }
    unsigned long code = rfTrigger.getReceivedValue();
    if (code == 0) {
        Serial.println("[RF] Не удалось декодировать код (нестандартный протокол).");
    } else {
        Serial.print("[RF] Код: ");
        Serial.print(code);
        Serial.print(" / Бит: ");
        Serial.print(rfTrigger.getReceivedBitlength());
        Serial.print(" / Протокол: ");
        Serial.print(rfTrigger.getReceivedProtocol());
        Serial.print(" / Юнит (pulse_us): ");
        Serial.print(rfTrigger.getReceivedDelay());
        Serial.println(" мкс");
    }
    rfTrigger.resetAvailable();
}

void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.println(">>> Buckshot Roulette IRL — ESP32 trigger");

    // Причина последней перезагрузки — чтобы в Serial было видно, что плата
    // ушла в ребут именно по watchdog (зависание), а не по питанию/кнопке.
    esp_reset_reason_t rr = esp_reset_reason();
    if (rr == ESP_RST_TASK_WDT || rr == ESP_RST_INT_WDT || rr == ESP_RST_WDT) {
        Serial.println("[WDT] Предыдущая загрузка: ПЕРЕЗАГРУЗКА ПО WATCHDOG (было зависание).");
    } else {
        Serial.print("[WDT] Причина перезагрузки: ");
        Serial.println((int)rr);
    }

    // Инициализируем Task Watchdog. Ядро Arduino уже могло его поднять, поэтому
    // сначала deinit (ошибку игнорируем, если не был инициализирован), затем
    // init с нужным таймаутом и trigger_panic=true — по срабатыванию чип
    // делает reboot, а не просто пишет предупреждение.
    esp_task_wdt_deinit();
    esp_task_wdt_config_t wdtCfg = {
        .timeout_ms = WDT_TIMEOUT_MS,
        .idle_core_mask = 0,       // idle-задачи не сторожим — только loop()
        .trigger_panic = true,
    };
    esp_task_wdt_init(&wdtCfg);
    esp_task_wdt_add(NULL);        // подписываем текущую задачу (loop)
    Serial.print("[WDT] Сторож включён, таймаут ");
    Serial.print(WDT_TIMEOUT_MS);
    Serial.println(" мс.");

    // LOW выставляем ещё до pinMode, чтобы не было случайного импульса
    // на MOSFET из-за неопределённого состояния пина при старте платы.
    digitalWrite(SOLENOID_PIN, LOW);
    pinMode(SOLENOID_PIN, OUTPUT);
    digitalWrite(SOLENOID_PIN, LOW);

    digitalWrite(LIVE_LED_PIN, LOW);
    pinMode(LIVE_LED_PIN, OUTPUT);
    digitalWrite(LIVE_LED_PIN, LOW);

    // Сначала поднимаем Wi-Fi, и только потом включаем RF-приём. Иначе
    // прерывания от шумящего приёмника сыплются во время критичного
    // WPA2-хендшейка и мешают ему завершиться.
    WiFi.mode(WIFI_STA);
    // Разовое сканирование при старте: печатает все видимые сети в Serial.
    scanNetworks();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    Serial.print("[WiFi] Подключаюсь к \"");
    Serial.print(WIFI_SSID);
    Serial.println("\"...");
    // Подключение целиком фоновое (см. ensureWifiConnected) — не блокируем
    // setup(), иначе watchdog ребутнёт плату раньше, чем поднимется сеть.
    Serial.println("[WiFi] Подключение идёт в фоне, вхожу в рабочий цикл.");

    // ── RF: быстрый ISR-декодер или режим обучения ──
    if (KNOWN_TRIGGER_CODE == 0UL) {
        rfTrigger.enableReceive(digitalPinToInterrupt(TRIGGER_PIN));
        Serial.println("[RF] Код пульта не задан — РЕЖИМ ОБУЧЕНИЯ. Жми кнопку пульта,");
        Serial.println("[RF] значения впиши в config.json (trigger_remote), перегенерируй config.h.");
    } else if (KNOWN_TRIGGER_PROTOCOL < 1 || KNOWN_TRIGGER_PROTOCOL > RF_PROTO_COUNT) {
        rfTrigger.enableReceive(digitalPinToInterrupt(TRIGGER_PIN));
        Serial.print("[RF] ОШИБКА: протокол ");
        Serial.print(KNOWN_TRIGGER_PROTOCOL);
        Serial.println(" не поддерживается быстрым декодером — работаю в режиме обучения.");
    } else {
        const RfProto &p = RF_PROTOS[KNOWN_TRIGGER_PROTOCOL - 1];
        rfZeroHi = p.zeroHi;
        rfZeroLo = p.zeroLo;
        rfOneHi = p.oneHi;
        rfOneLo = p.oneLo;
        rfSyncDiv = (p.syncLo > p.syncHi) ? p.syncLo : p.syncHi;  // длинная часть sync
        rfSkipInit = p.inverted ? 1 : 0;
        fastRfActive = true;

        // Задача триггера — ДО attachInterrupt (ISR будит её по handle).
        // Приоритет 10 (loop() имеет 1), ядро 0 — отдельно от loop(): даже если
        // тот повис в блокирующем HTTP, импульс выключится вовремя.
        xTaskCreatePinnedToCore(triggerTask, "rf_trigger", 4096, NULL, 10, &triggerTaskHandle, 0);

        pinMode(TRIGGER_PIN, INPUT);
        attachInterrupt(digitalPinToInterrupt(TRIGGER_PIN), rfIsr, CHANGE);

        Serial.printf("[RF] Быстрый ISR-декодер: код %lu, протокол %d, %d бит.\n",
                      (unsigned long)KNOWN_TRIGGER_CODE,
                      (int)KNOWN_TRIGGER_PROTOCOL,
                      (int)KNOWN_TRIGGER_BITLENGTH);
        if (RF_PULSE_US) {
            Serial.printf("[RF] Юнит задан: %lu мкс — срабатывание с ПЕРВОГО кадра пачки.\n",
                          (unsigned long)RF_PULSE_US);
        } else {
            Serial.println("[RF] Юнит не задан (pulse_us=0) — вычисляю из sync, срабатывание со 2-го кадра.");
            Serial.println("[RF] Подсказка: впиши pulse_us из режима обучения — станет ещё быстрее.");
        }
    }
}

void loop() {
    // Горячий путь курка живёт в ISR/задаче триггера — loop() занимается только
    // фоном: Wi-Fi, опрос сервера, LED, отложенное уведомление о выстреле.
    if (!fastRfActive) {
        handleRfLearning();
    }

    ensureWifiConnected();

    // Подстраховка к задаче триггера: соленоид физически не может провисеть
    // в HIGH дольше SOLENOID_MAX_ON_MS, даже если что-то в коде застряло.
    if (solenoidOn && (uint32_t)(micros() - solenoidOnSinceUs) > SOLENOID_MAX_ON_MS * 1000UL) {
        Serial.println("[SAFETY] Превышен лимит удержания соленоида — принудительно выключаю.");
        setSolenoid(false);
    }

    // Предохранитель блокировки соленоида: если дилер так и не выбрал цель или
    // POST /shoot не дошёл до сервера, снимаем awaitingTarget по таймауту, чтобы
    // курок не залип заблокированным навсегда.
    if (awaitingTarget &&
        (uint32_t)(micros() - awaitingSinceUs) > AWAIT_TARGET_TIMEOUT_MS * 1000UL) {
        awaitingTarget = false;
        pendingSeen = false;
        Serial.println("[КУРОК] Таймаут ожидания выбора цели — соленоид разблокирован принудительно.");
    }

    // Фоновый опрос статуса патрона для кэша ISR и LED-индикатора.
    if (millis() - lastPollMs >= POLL_INTERVAL_MS) {
        pollShellStatus();
    }

    updateLiveLed();

    // Отложенное уведомление сервера о выстреле (HTTP вне горячего пути).
    if (pendingShoot) {
        pendingShoot = false;
        sendShootToServer();
    }

    // «Кормим» сторож в самом конце прохода: если loop где-то выше завис
    // дольше WDT_TIMEOUT_MS и сюда не дошёл — watchdog перезагрузит плату.
    esp_task_wdt_reset();
}
