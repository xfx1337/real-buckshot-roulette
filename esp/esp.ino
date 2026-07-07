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
static const unsigned long POLL_INTERVAL_MS = 1000;      // как часто спрашивать сервер о статусе патрона
static const unsigned long HTTP_TIMEOUT_MS = 400;        // короткий таймаут, чтобы не подвесить loop()
static const unsigned long SOLENOID_PULSE_MS = 150;      // длительность импульса на соленоид (100-200ms)
static const unsigned long SOLENOID_MAX_ON_MS = 500;     // аварийный предел — жёстко выключаем после него
static const unsigned long TRIGGER_DEBOUNCE_MS = 250;    // минимальный интервал между выстрелами
static const unsigned long WIFI_RETRY_INTERVAL_MS = 5000;
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
    if (WiFi.status() == WL_CONNECTED) {
        return;
    }
    unsigned long now = millis();
    if (now - lastWifiAttemptMs < WIFI_RETRY_INTERVAL_MS) {
        return;
    }
    lastWifiAttemptMs = now;
    Serial.print("[WiFi] Не подключено (статус: ");
    Serial.print(wifiStatusToStr(WiFi.status()));
    Serial.println("), пробую (пере)подключиться...");
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

void pollShellStatus() {
    if (WiFi.status() != WL_CONNECTED) {
        cachedReady = false;
        return;
    }

    HTTPClient http;
    http.setTimeout(HTTP_TIMEOUT_MS);
    String url = String(SERVER_BASE_URL) + "/api/esp/shell_status";
    if (!http.begin(url)) {
        cachedReady = false;
        return;
    }

    int code = http.GET();
    if (code == HTTP_CODE_OK) {
        String body = http.getString();
        // Крошечный JSON вида {"ready": true, "live": false} — парсим руками,
        // чтобы не тащить в проект библиотеку ради двух булевых полей.
        bool ready = body.indexOf("\"ready\": true") >= 0 || body.indexOf("\"ready\":true") >= 0;
        bool live = body.indexOf("\"live\": true") >= 0 || body.indexOf("\"live\":true") >= 0;
        cachedReady = ready;
        cachedLive = live;
    } else {
        Serial.print("[HTTP] Ошибка опроса статуса патрона, код: ");
        Serial.println(code);
        cachedReady = false;
    }
    http.end();
}

void fireTrigger() {
    unsigned long now = millis();

    // Программный дебаунс: игнорируем повторные срабатывания в течение
    // TRIGGER_DEBOUNCE_MS после последнего выстрела/попытки.
    if (now - lastTriggerMs < TRIGGER_DEBOUNCE_MS) {
        return;
    }
    lastTriggerMs = now;

    if (cachedReady && cachedLive) {
        Serial.println(">>> Курок: боевой патрон — импульс на соленоид!");
        setSolenoid(true);
        delay(SOLENOID_PULSE_MS);
        setSolenoid(false);
    } else {
        Serial.println(">>> Курок: холостой (или сервер недоступен/не фаза стрельбы) — тишина.");
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
        // Режим обучения: печатаем всё, что поймали, чтобы узнать код пульта-курка.
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
    } else if (code == KNOWN_TRIGGER_CODE && protocol == KNOWN_TRIGGER_PROTOCOL
               && bitLength == KNOWN_TRIGGER_BITLENGTH) {
        fireTrigger();
    }
    // Сигналы с чужих пультов молча игнорируются.

    rfTrigger.resetAvailable();
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

    rfTrigger.enableReceive(digitalPinToInterrupt(TRIGGER_PIN));

    digitalWrite(LIVE_LED_PIN, LOW);
    pinMode(LIVE_LED_PIN, OUTPUT);
    digitalWrite(LIVE_LED_PIN, LOW);

    WiFi.mode(WIFI_STA);
    WiFi.disconnect();
    delay(100);
    scanNetworks();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

void loop() {
    ensureWifiConnected();

    // Аварийная защита: соленоид физически не может провисеть в HIGH
    // дольше SOLENOID_MAX_ON_MS, даже если что-то в коде застряло.
    if (solenoidOn && millis() - solenoidOnSinceMs > SOLENOID_MAX_ON_MS) {
        Serial.println("[SAFETY] Превышен лимит удержания соленоида — принудительно выключаю.");
        setSolenoid(false);
    }

    unsigned long now = millis();
    if (now - lastPollMs >= POLL_INTERVAL_MS) {
        lastPollMs = now;
        pollShellStatus();
    }

    updateLiveLed();

    handleRfTrigger();
}
