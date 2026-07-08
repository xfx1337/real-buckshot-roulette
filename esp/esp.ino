/*
  Buckshot Roulette IRL — физический триггер соленоида (ESP32).

  Логика:
    - Раз в секунду опрашиваем GET /api/esp/shell_status и кэшируем
      ответ (live: true/false, ready: true/false) в памяти.
    - При нажатии курка (RF-пульт 433МГц, приёмник на GPIO TRIGGER_PIN)
      решение — стрелять или нет — принимается МГНОВЕННО по закэшированному
      значению, без сетевого запроса. Это устраняет задержку сети в момент
      выстрела.
    - Соленоид (GPIO SOLENOID_PIN) активируется, только если последний
      опрос вернул ready=true и live=true. Если патрон холостой, ready=false
      (не фаза стрельбы/магазин пуст) — соленоид не срабатывает вообще.
    - Сервер остаётся источником истины для игровой логики: выстрел (урон,
      ход) по-прежнему делает дилер кнопкой в веб-интерфейсе. Эта прошивка
      только физически бабахает затвором синхронно с реальным патроном.
    - Пока закэшированный статус "следующий патрон боевой" (ready && live),
      лампочка на LIVE_LED_PIN (D2) мигает — визуальная индикация без
      обращения к серверу, обновляется тем же кэшем, что и соленоид.

  Требования ТЗ:
    - Debounce курка (программный, с блокировкой на время импульса).
    - Safety timeout: соленоид не может физически зависнуть в HIGH дольше
      SOLENOID_MAX_ON_MS, даже если в коде где-то зависла логика.
    - Wi-Fi: автопереподключение, не блокирующее loop().
    - Курок физически — RF-пульт 433МГц (не механическая кнопка): приёмник
      висит на TRIGGER_PIN и декодирует код через RCSwitch. Пока код пульта
      неизвестен (KNOWN_TRIGGER_CODE == 0), прошивка работает в режиме
      обучения — печатает в Serial любой пойманный код, чтобы его можно было
      вписать в константы ниже. Это устраняет ложные срабатывания от шума
      эфира, из-за которых raw digitalRead() на этом пине забивал loop()
      и мешал Wi-Fi стеку подключиться.
*/

#include <WiFi.h>
#include <HTTPClient.h>
#include <RCSwitch.h>

// Все настройки (Wi-Fi, адрес сервера, пины, код пульта-курка) берутся из
// config.h, который генерируется из корневого config.json скриптом
// esp/gen_config.py. Перед прошивкой выполни:  python esp/gen_config.py
//
// Чтобы узнать код нового пульта: поставь "code": 0 в config.json, перегенерируй
// config.h, прошей — прошивка в режиме обучения печатает пойманный код в Serial.
// Затем впиши код в config.json, снова перегенерируй и перепрошей.
#include "config.h"

RCSwitch rfTrigger = RCSwitch();

// ── Тайминги ──
static const unsigned long POLL_INTERVAL_MS = 5000;      // фоновый опрос для LED-индикатора (компромисс: реже = меньше конфликта с RF)
static const unsigned long HTTP_TIMEOUT_MS = 1500;       // таймаут HTTP; 400мс было мало для первого TCP-хендшейка
static const unsigned long SOLENOID_PULSE_MS = 250;      // длительность импульса на соленоид (100-200ms)
static const unsigned long SOLENOID_MAX_ON_MS = 500;     // аварийный предел — жёстко выключаем после него
static const unsigned long TRIGGER_DEBOUNCE_MS = 250;    // минимальный интервал между выстрелами
static const unsigned long WIFI_RETRY_INTERVAL_MS = 10000;  // даём WPA2-хендшейку время завершиться перед повтором
static const unsigned long LIVE_LED_BLINK_MS = 300;      // период мигания лампочки D2

// ── Состояние ──
bool cachedReady = false;
bool cachedLive = false;
unsigned long lastPollMs = 0;
unsigned long lastTriggerMs = 0;
unsigned long lastWifiAttemptMs = 0;

bool solenoidOn = false;
unsigned long solenoidOnSinceMs = 0;

bool liveLedOn = false;
unsigned long lastLiveLedToggleMs = 0;

// Флаг «нужно сообщить серверу о выстреле». Ставится в fireTrigger() (в
// обработчике курка), а сам HTTP-запрос уходит в loop() — чтобы не делать
// блокирующий сетевой вызов внутри обработки RF.
bool pendingShoot = false;

void updateLiveLed() {
    if (!(cachedReady && cachedLive)) {
        if (liveLedOn) {
            liveLedOn = false;
            digitalWrite(LIVE_LED_PIN, LOW);
        }
        return;
    }

    unsigned long now = millis();
    if (now - lastLiveLedToggleMs >= LIVE_LED_BLINK_MS) {
        lastLiveLedToggleMs = now;
        liveLedOn = !liveLedOn;
        digitalWrite(LIVE_LED_PIN, liveLedOn ? HIGH : LOW);
    }
}

void setSolenoid(bool on) {
    solenoidOn = on;
    digitalWrite(SOLENOID_PIN, on ? HIGH : LOW);
    if (on) {
        solenoidOnSinceMs = millis();
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
        cachedReady = ready;
        cachedLive = live;
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
// статусом следующего патрона. Вызывается из loop(), не из обработчика курка.
void sendShootToServer() {
    if (WiFi.status() != WL_CONNECTED) {
        return;
    }

    HTTPClient http;
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

void fireTrigger() {
    unsigned long now = millis();

    // Программный дебаунс: игнорируем повторные срабатывания в течение
    // TRIGGER_DEBOUNCE_MS после последнего выстрела/попытки.
    if (now - lastTriggerMs < TRIGGER_DEBOUNCE_MS) {
        return;
    }
    lastTriggerMs = now;

    // Решение о соленоиде принимаем МГНОВЕННО по закэшированному статусу
    // (обновляется фоновым опросом). Синхронный HTTP-запрос здесь делать
    // НЕЛЬЗЯ: блокирующий вызов вешает loop(), рвёт Wi-Fi и роняет плату по
    // watchdog. Поэтому уведомление сервера откладываем через pendingShoot.
    if (cachedReady && cachedLive) {
        Serial.println(">>> Курок: боевой патрон — импульс на соленоид!");
        setSolenoid(true);
        delay(SOLENOID_PULSE_MS);
        setSolenoid(false);
    } else {
        Serial.println(">>> Курок: холостой (или сервер недоступен/не фаза стрельбы) — тишина.");
    }

    // Патрон продвигается в любом случае (курок спущен = патрон ушёл), поэтому
    // просим сервер вытолкнуть текущий патрон независимо от боевой/холостой.
    if (cachedReady) {
        pendingShoot = true;
    }
}

// Сливает (отбрасывает) все накопленные в буфере RCSwitch пакеты. Одно нажатие
// пульта 433МГц физически шлёт код ПАЧКОЙ из нескольких одинаковых пакетов, и
// все они оседают в буфере приёмника. После того как первый пакет уже признан
// валидным и запустил выстрел, остальные из той же пачки — мусор: их надо
// выбросить, иначе они будут разбираться в последующих проходах loop() (и,
// хотя fireTrigger() дебаунсит по времени, зря нагружать парсинг и держать
// буфер занятым). Так один физический нажим = ровно один валидный пакет.
void drainRfBuffer() {
    while (rfTrigger.available()) {
        rfTrigger.resetAvailable();
    }
}

void handleRfTrigger() {
    if (!rfTrigger.available()) {
        return;
    }

    unsigned long code = rfTrigger.getReceivedValue();
    unsigned int protocol = rfTrigger.getReceivedProtocol();
    unsigned int bitLength = rfTrigger.getReceivedBitlength();

    if (KNOWN_TRIGGER_CODE == 0) {
        // Режим обучения: печатаем пойманный код, чтобы вписать его в config.json.
        if (code == 0) {
            Serial.println("[RF] Не удалось декодировать код (нестандартный протокол).");
        } else {
            Serial.print("[RF] Код: ");
            Serial.print(code);
            Serial.print(" / Бит: ");
            Serial.print(bitLength);
            Serial.print(" / Протокол: ");
            Serial.println(protocol);
        }
        rfTrigger.resetAvailable();
    } else if (code == KNOWN_TRIGGER_CODE && protocol == KNOWN_TRIGGER_PROTOCOL
               && bitLength == KNOWN_TRIGGER_BITLENGTH) {
        // Валидация по ПЕРВОМУ пакету пачки: он один запускает выстрел, а все
        // остальные повторы этого же нажатия сразу выбрасываем из буфера.
        fireTrigger();
        drainRfBuffer();
    } else {
        // Сигнал с чужого пульта — молча выбрасываем этот пакет.
        rfTrigger.resetAvailable();
    }
}

void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.println(">>> Buckshot Roulette IRL — ESP32 trigger");

    // LOW выставляем ещё до pinMode, чтобы не было случайного импульса
    // на MOSFET из-за неопределённого состояния пина при старте платы.
    digitalWrite(SOLENOID_PIN, LOW);
    pinMode(SOLENOID_PIN, OUTPUT);
    digitalWrite(SOLENOID_PIN, LOW);

    digitalWrite(LIVE_LED_PIN, LOW);
    pinMode(LIVE_LED_PIN, OUTPUT);
    digitalWrite(LIVE_LED_PIN, LOW);

    // Сначала поднимаем Wi-Fi, и только потом включаем RF-приёмник. Иначе
    // прерывания от шумящего приёмника на GPIO4 сыплются во время критичного
    // WPA2-хендшейка и мешают ему завершиться.
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    Serial.print("[WiFi] Подключаюсь к \"");
    Serial.print(WIFI_SSID);
    Serial.println("\"...");

    // Даём хендшейку шанс завершиться до входа в loop (до ~8 секунд).
    unsigned long startAttempt = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - startAttempt < 8000) {
        delay(250);
        Serial.print(".");
    }
    Serial.println();

    rfTrigger.enableReceive(digitalPinToInterrupt(TRIGGER_PIN));
}

void loop() {
    ensureWifiConnected();

    // Аварийная защита: соленоид физически не может провисеть в HIGH
    // дольше SOLENOID_MAX_ON_MS, даже если что-то в коде застряло.
    if (solenoidOn && millis() - solenoidOnSinceMs > SOLENOID_MAX_ON_MS) {
        Serial.println("[SAFETY] Превышен лимит удержания соленоида — принудительно выключаю.");
        setSolenoid(false);
    }

    // Фоновый опрос для LED-индикатора. lastPollMs обновляется внутри
    // pollShellStatus(), поэтому выстрел (тоже вызывающий опрос) сдвигает
    // следующий фоновый опрос, а не дублирует запрос сразу после.
    if (millis() - lastPollMs >= POLL_INTERVAL_MS) {
        pollShellStatus();
    }

    updateLiveLed();

    handleRfTrigger();

    // Отложенное уведомление сервера о выстреле (HTTP вне обработчика курка).
    if (pendingShoot) {
        pendingShoot = false;
        sendShootToServer();
    }
}
