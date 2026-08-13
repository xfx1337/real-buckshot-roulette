/*
  Buckshot Roulette IRL — физический триггер соленоида (ESP32).
  Архитектура (максимально быстрая реакция на курок):
    - Приём сигнала выстрела через ESP-NOW (минимальная задержка).
    - При получении совпадения кода (KNOWN_TRIGGER_CODE) соленоид включается 
      почти мгновенно, прямой записью в GPIO-регистр.
    - Выключение соленоида, лог и уведомление сервера делает отдельная
      FreeRTOS-задача с высоким приоритетом (просыпается по notify из ESP-NOW коллбека).
    - «Глухое» окно RF_LOCKOUT_MS после срабатывания поглощает повторные пакеты.
*/

#include <WiFi.h>
#include <HTTPClient.h>
#include <esp_now.h>
#include <esp_task_wdt.h>
#include <soc/gpio_struct.h>

#include "config.h"

// ── Тайминги и пороги ──
static const unsigned long POLL_INTERVAL_MS = CFG_POLL_INTERVAL_MS;
static const unsigned long HTTP_TIMEOUT_MS = CFG_HTTP_TIMEOUT_MS;
static const unsigned long SOLENOID_PULSE_MS = CFG_SOLENOID_PULSE_MS;
static const unsigned long SOLENOID_MAX_ON_MS = CFG_SOLENOID_MAX_ON_MS;
static const unsigned long RF_LOCKOUT_MS = CFG_RF_LOCKOUT_MS;
static const unsigned long WIFI_RETRY_INTERVAL_MS = CFG_WIFI_RETRY_INTERVAL_MS;
static const unsigned long LIVE_LED_BLINK_MS = CFG_LIVE_LED_BLINK_MS;
static const uint32_t WDT_TIMEOUT_MS = CFG_WDT_TIMEOUT_MS;

static_assert(SOLENOID_PIN < 32, "SOLENOID_PIN должен быть GPIO 0-31");
static const uint32_t SOLENOID_BIT = (1UL << SOLENOID_PIN);

// ── Состояние (loop-контекст) ──
volatile bool cachedReady = false;
volatile bool cachedLive = false;
// awaitingTarget больше не нужен: выстрелы ВСЕГДА
// пробрасываются на сервер (и при калибровке, и при игре).
unsigned long lastPollMs = 0;
unsigned long lastWifiAttemptMs = 0;

volatile bool solenoidOn = false;
volatile uint32_t solenoidOnSinceUs = 0;

volatile bool pendingShoot = false;
unsigned long lastShootAttemptMs = 0;
volatile float pendingAngle = 0.0;
volatile float pendingPitch = 0.0;
volatile uint32_t pendingShotId = 0;
uint8_t shootAttemptCount = 0;
// ── Состояние выстрела ──
static volatile uint32_t lastSeenUs = (uint32_t)(0UL - CFG_RF_LOCKOUT_MS * 1000UL);
static volatile uint32_t lastFireUs = 0;

static TaskHandle_t triggerTaskHandle = NULL;
static volatile bool fireIsLive = false;
static volatile uint32_t fireDecodedUs = 0;

typedef struct {
    uint32_t triggerCode;
    float angle;
    float pitch;
} __attribute__((packed)) esp_now_msg_t;

// Решение по валидному коду (вызывается из коллбека ESP-NOW)
static void IRAM_ATTR espNowFire(uint32_t nowUs) {
    uint32_t sinceSeen = nowUs - lastSeenUs;
    if (sinceSeen < RF_LOCKOUT_MS * 1000UL) {
        return;
    }
    lastSeenUs = nowUs;

    lastFireUs = nowUs;
    fireIsLive = cachedReady && cachedLive;
    fireDecodedUs = nowUs;

    // Соленоид щёлкает ТОЛЬКО если патрон боевой
    if (fireIsLive) {
        GPIO.out_w1ts = SOLENOID_BIT;
        solenoidOn = true;
        solenoidOnSinceUs = nowUs;
    }

    // ВСЕГДА отправляем выстрел на сервер (и при калибровке, и при игре)
    pendingShoot = true;
    pendingShotId = nowUs;
    lastShootAttemptMs = 0; // Сбрасываем таймер для немедленной отправки
    shootAttemptCount = 0;

    BaseType_t woken = pdFALSE;
    vTaskNotifyGiveFromISR(triggerTaskHandle, &woken);
    if (woken == pdTRUE) {
        portYIELD_FROM_ISR();
    }
}

// ESP-NOW Receive Callback
#if ESP_ARDUINO_VERSION_MAJOR >= 3
void OnDataRecv(const esp_now_recv_info_t *esp_now_info, const uint8_t *incomingData, int len) {
#else
void OnDataRecv(const uint8_t *mac, const uint8_t *incomingData, int len) {
#endif
    if (len >= (int)sizeof(uint32_t)) {
        esp_now_msg_t msg;
        memcpy(&msg, incomingData, len > (int)sizeof(msg) ? sizeof(msg) : len);
        
        if (msg.triggerCode == KNOWN_TRIGGER_CODE) {
            if (len >= (int)sizeof(esp_now_msg_t)) {
                pendingAngle = msg.angle;
                pendingPitch = msg.pitch;
            } else if (len >= (int)sizeof(uint32_t) + sizeof(float)) {
                pendingAngle = msg.angle;
                pendingPitch = 0.0;
            } else {
                pendingAngle = -1.0;
                pendingPitch = 0.0;
            }
            Serial.printf(">>> [ESP-NOW RECV] СИГНАЛ ПРИНЯТ! Угол=%.1f° | Наклон=%.1f° | Код=%u\n", 
                          pendingAngle, pendingPitch, msg.triggerCode);
            espNowFire(micros());
        } else {
            Serial.printf("[ESP-NOW RECV] Проигнорирован неизвестный triggerCode: %u (ожидался %u)\n", 
                          msg.triggerCode, KNOWN_TRIGGER_CODE);
        }
    } else {
        Serial.printf("[ESP-NOW RECV] Слишком короткий пакет: %d байт\n", len);
    }
}

// Задача триггера — только управление таймингом соленоида
void triggerTask(void *) {
    for (;;) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        uint32_t wakeUs = micros();
        bool live = fireIsLive;
        uint32_t decodedUs = fireDecodedUs;

        if (live) {
            Serial.println(">>> Курок: боевой патрон — соленоид включён!");
            Serial.printf("[ЗАМЕР] ESP-NOW callback → задача: %lu мкс\n",
                          (unsigned long)(wakeUs - decodedUs));
            uint32_t elapsedMs = (micros() - decodedUs) / 1000;
            if (elapsedMs < SOLENOID_PULSE_MS) {
                vTaskDelay(pdMS_TO_TICKS(SOLENOID_PULSE_MS - elapsedMs));
            }
            GPIO.out_w1tc = SOLENOID_BIT;
            solenoidOn = false;
        } else {
            Serial.println(">>> Курок: холостой (или калибровка).");
        }
    }
}

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
        case WL_NO_SSID_AVAIL:   return "NO_SSID_AVAIL";
        case WL_SCAN_COMPLETED:  return "SCAN_COMPLETED";
        case WL_CONNECTED:       return "CONNECTED";
        case WL_CONNECT_FAILED:  return "CONNECT_FAILED";
        case WL_CONNECTION_LOST: return "CONNECTION_LOST";
        case WL_DISCONNECTED:    return "DISCONNECTED";
        default:                 return "UNKNOWN";
    }
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
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

bool pollShellStatus() {
    lastPollMs = millis();
    if (WiFi.status() != WL_CONNECTED) {
        cachedReady = false;
        return false;
    }
    HTTPClient http;
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
        bool ready = body.indexOf("\"ready\": true") >= 0 || body.indexOf("\"ready\":true") >= 0;
        bool live = body.indexOf("\"live\": true") >= 0 || body.indexOf("\"live\":true") >= 0;
        
        cachedLive = live;
        cachedReady = ready;

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

bool sendShootToServer() {
    if (WiFi.status() != WL_CONNECTED) {
        return false;
    }
    HTTPClient http;
    http.setConnectTimeout(HTTP_TIMEOUT_MS);
    http.setTimeout(HTTP_TIMEOUT_MS);
    String url = String(SERVER_BASE_URL) + "/api/esp/shoot";
    if (!http.begin(url)) {
        return false;
    }
    http.addHeader("Content-Type", "application/x-www-form-urlencoded");
    
    // Безопасно копируем значения
    float localAngle = pendingAngle;
    float localPitch = pendingPitch;
    uint32_t localShotId = pendingShotId;
    
    char payload[128];
    snprintf(payload, sizeof(payload), "angle=%.1f&pitch=%.1f&shot_id=%u", localAngle, localPitch, localShotId);
    
    int code = http.POST(payload);
    bool success = false;
    if (code == HTTP_CODE_OK) {
        Serial.printf("[HTTP] Выстрел зарегистрирован. Угол: %.1f°, Наклон: %.1f°\n", localAngle, localPitch);
        success = true;
    } else {
        Serial.print("[HTTP] Ошибка регистрации, код: ");
        Serial.println(code);
    }
    http.end();
    pollShellStatus();
    return success;
}

void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.println(">>> Buckshot Roulette IRL — ESP32 Receiver (ESP-NOW)");

    esp_task_wdt_deinit();
    esp_task_wdt_config_t wdtCfg = {
        .timeout_ms = WDT_TIMEOUT_MS,
        .idle_core_mask = 0,
        .trigger_panic = true,
    };
    esp_task_wdt_init(&wdtCfg);
    esp_task_wdt_add(NULL);

    digitalWrite(SOLENOID_PIN, LOW);
    pinMode(SOLENOID_PIN, OUTPUT);
    digitalWrite(SOLENOID_PIN, LOW);

    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false); // Запрещаем сон Wi-Fi, чтобы не пропускать ESP-NOW пакеты от дробовика
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    Serial.println("[WiFi] Подключение идёт в фоне...");

    // Инициализация ESP-NOW
    if (esp_now_init() != ESP_OK) {
        Serial.println("[ESP-NOW] Ошибка инициализации!");
    } else {
        esp_now_register_recv_cb(OnDataRecv);
        Serial.println("[ESP-NOW] Инициализирован. Жду сигналы.");
    }

    xTaskCreatePinnedToCore(triggerTask, "esp_trigger", 4096, NULL, 10, &triggerTaskHandle, 0);
}

void loop() {
    ensureWifiConnected();

    if (solenoidOn && (uint32_t)(micros() - solenoidOnSinceUs) > SOLENOID_MAX_ON_MS * 1000UL) {
        Serial.println("[SAFETY] Превышен лимит удержания соленоида — принудительно выключаю.");
        setSolenoid(false);
    }

    if (millis() - lastPollMs >= POLL_INTERVAL_MS) {
        pollShellStatus();
    }

    if (pendingShoot) {
        if (lastShootAttemptMs == 0 || millis() - lastShootAttemptMs > 500) {
            lastShootAttemptMs = millis() == 0 ? 1 : millis();
            shootAttemptCount++;
            if (sendShootToServer() || shootAttemptCount >= 3) {
                pendingShoot = false;
            } else {
                Serial.printf("[HTTP] Ошибка доставки, попытка %d/3 через 500мс...\n", shootAttemptCount);
            }
        }
    }

    esp_task_wdt_reset();
}
