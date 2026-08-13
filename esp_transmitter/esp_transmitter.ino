#include <ESP8266WiFi.h>
#include <espnow.h>
#include "config.h"
#include <Wire.h>

#undef TRIGGER_PIN
#define TRIGGER_PIN D5 // GPIO14 на Wemos D1 Mini

typedef struct {
    uint32_t triggerCode;
    float angle; // Отслеживаемый угол (0..360°)
    float pitch; // Наклон ствола в градусах (-90°..+90°)
} __attribute__((packed)) esp_now_msg_t;

uint8_t broadcastAddress[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

// ── Аппаратное прерывание курка ──
volatile bool triggerFired = false;
volatile unsigned long triggerFireMs = 0;

ICACHE_RAM_ATTR void triggerISR() {
    unsigned long now = millis();
    if (now - triggerFireMs > 80) {  // 80мс lockout от дребезга
        triggerFired = true;
        triggerFireMs = now;
    }
}

void OnDataSent(uint8_t *mac_addr, uint8_t status) {
    // Не спамим в консоль при каждом успешном пакете, чтобы не тормозить цикл
    if (status != 0) {
        Serial.println("[ESP-NOW] ОШИБКА ДОСТАВКИ");
    }
}

bool readMPU6050(float &ax, float &ay, float &az, float &gx, float &gy, float &gz) {
    Wire.beginTransmission(0x68);
    Wire.write(0x3B);
    if (Wire.endTransmission(false) == 0 && Wire.requestFrom(0x68, 14) == 14) {
        int16_t rawAX = (Wire.read() << 8) | Wire.read();
        int16_t rawAY = (Wire.read() << 8) | Wire.read();
        int16_t rawAZ = (Wire.read() << 8) | Wire.read();
        Wire.read(); Wire.read(); // Пропускаем температуру
        int16_t rawGX = (Wire.read() << 8) | Wire.read();
        int16_t rawGY = (Wire.read() << 8) | Wire.read();
        int16_t rawGZ = (Wire.read() << 8) | Wire.read();
        
        ax = (float)rawAX / 16384.0f;
        ay = (float)rawAY / 16384.0f;
        az = (float)rawAZ / 16384.0f;
        gx = (float)rawGX / 131.0f; // 131 LSB/(deg/s) для диапазона +/- 250 deg/s
        gy = (float)rawGY / 131.0f;
        gz = (float)rawGZ / 131.0f;
        return true;
    }
    return false;
}

float gyroXOffset = 0, gyroYOffset = 0, gyroZOffset = 0;

void setup() {
    Serial.begin(115200);
    Wire.begin();
    Wire.setClock(400000); // 400kHz Fast I2C для минимальной задержки чтения
    
    // 1. Пробуждаем MPU6050 (0x68)
    Wire.beginTransmission(0x68);
    Wire.write(0x6B); // PWR_MGMT_1
    Wire.write(0x00); // Wake up
    Wire.endTransmission();
    delay(50);

    // 2. Настраиваем гироскоп на +/- 250 deg/s (максимальная точность)
    Wire.beginTransmission(0x68);
    Wire.write(0x1B); // GYRO_CONFIG
    Wire.write(0x00); 
    Wire.endTransmission();
    delay(50);

    Serial.println("\n\n[СЕНСОРЫ] Калибровка гироскопа (ПОЛОЖИ ДРОБОВИК И НЕ ТРОГАЙ 2 СЕК!)...");
    float sumGx = 0, sumGy = 0, sumGz = 0;
    int samples = 0;
    for (int i = 0; i < 200; i++) {
        float ax, ay, az, gx, gy, gz;
        if (readMPU6050(ax, ay, az, gx, gy, gz)) {
            sumGx += gx; sumGy += gy; sumGz += gz;
            samples++;
        }
        delay(10);
    }
    if (samples > 0) {
        gyroXOffset = sumGx / samples;
        gyroYOffset = sumGy / samples;
        gyroZOffset = sumGz / samples;
    }
    Serial.printf("[СЕНСОРЫ] Смещения: X=%.2f Y=%.2f Z=%.2f\n", gyroXOffset, gyroYOffset, gyroZOffset);
    Serial.println("[СЕНСОРЫ] Калибровка гироскопа завершена УСПЕШНО.");

    pinMode(TRIGGER_PIN, INPUT_PULLUP);
    attachInterrupt(digitalPinToInterrupt(TRIGGER_PIN), triggerISR, FALLING);
    
    WiFi.mode(WIFI_STA);
    
    Serial.println("[WIFI] Поиск сети...");
    int n = WiFi.scanNetworks();
    int channel = 1;
    for (int i = 0; i < n; i++) {
        if (WiFi.SSID(i) == WIFI_SSID) {
            channel = WiFi.channel(i);
            break;
        }
    }
    Serial.printf("[WIFI] Сеть %s найдена на канале %d\n", WIFI_SSID, channel);
    WiFi.scanDelete();

    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    Serial.print("[WIFI] Подключение ");
    while (WiFi.status() != WL_CONNECTED) {
        delay(100);
        Serial.print(".");
    }
    Serial.println("\n[WIFI] Подключено, канал зафиксирован.");

    if (esp_now_init() != 0) {
        Serial.println("[ESP-NOW] Ошибка инициализации");
        return;
    }
    
    esp_now_set_self_role(ESP_NOW_ROLE_CONTROLLER);
    esp_now_register_send_cb(OnDataSent);
    
    if (esp_now_add_peer(broadcastAddress, ESP_NOW_ROLE_SLAVE, channel, NULL, 0) != 0){
        Serial.println("[ESP-NOW] Ошибка добавления пира");
        return;
    }
    Serial.println("\n==================================");
    Serial.println("[ТРАНСМИТТЕР] ГОТОВ К РАБОТЕ!");
    Serial.println("==================================");
}


float lastKnownAngle = 0.0f;
float lastKnownPitch = 0.0f;

static float filtGx = 0, filtGy = 0, filtGz = 1.0f;
static bool gravityInitialized = false;
const float GRAVITY_ALPHA = 0.05f;

unsigned long lastLoopTime = 0;

void loop() {
    unsigned long now = micros();
    if (lastLoopTime == 0) lastLoopTime = now;
    float dt = (now - lastLoopTime) / 1000000.0f;
    lastLoopTime = now;

    float ax = 0, ay = 0, az = 0, gx = 0, gy = 0, gz = 0;
    if (readMPU6050(ax, ay, az, gx, gy, gz)) {
        
        // 1. Применяем калибровочные смещения к гироскопу
        gx -= gyroXOffset;
        gy -= gyroYOffset;
        gz -= gyroZOffset;

        // 2. EMA-фильтр акселерометра (выделяем вектор гравитации "вниз")
        if (!gravityInitialized) {
            filtGx = ax; filtGy = ay; filtGz = az;
            gravityInitialized = true;
        } else {
            filtGx = GRAVITY_ALPHA * ax + (1.0f - GRAVITY_ALPHA) * filtGx;
            filtGy = GRAVITY_ALPHA * ay + (1.0f - GRAVITY_ALPHA) * filtGy;
            filtGz = GRAVITY_ALPHA * az + (1.0f - GRAVITY_ALPHA) * filtGz;
        }

        // 3. Вычисляем Pitch (наклон ствола) для сервера
        float pitchRad = atan2(-filtGx, sqrt(filtGy * filtGy + filtGz * filtGz));
        float pitchX_deg = pitchRad * 180.0f / PI;
        float pitchY_deg = atan2(filtGy, sqrt(filtGx * filtGx + filtGz * filtGz)) * 180.0f / PI;
        lastKnownPitch = (abs(pitchX_deg) > abs(pitchY_deg)) ? pitchX_deg : pitchY_deg;

        // 4. Интеграция гироскопа (виртуальный компас)
        // Нормализуем гравитацию
        float gLen = sqrt(filtGx*filtGx + filtGy*filtGy + filtGz*filtGz);
        float nx = 0, ny = 0, nz = 1.0f;
        if (gLen > 0.01f) {
            nx = filtGx / gLen;
            ny = filtGy / gLen;
            nz = filtGz / gLen;
        }

        // Вычисляем угловую скорость вращения вокруг МИРОВОЙ ВЕРТИКАЛИ (yaw).
        // Это скалярное произведение вектора (gx, gy, gz) и вектора "вверх" (-nx, -ny, -nz).
        float yawRate = -(gx * nx + gy * ny + gz * nz);
        
        // Интегрируем угол, применяя небольшую мертвую зону (отсекаем микро-шум)
        if (abs(yawRate) > 0.5f) {
            lastKnownAngle += yawRate * dt;
            
            // Держим угол в диапазоне 0..360
            while (lastKnownAngle >= 360.0f) lastKnownAngle -= 360.0f;
            while (lastKnownAngle < 0.0f) lastKnownAngle += 360.0f;
        }
    }



    // Выстрел: аппаратное прерывание ловит любой клик
    if (triggerFired) {
        triggerFired = false;
        Serial.printf(">>> [КУРОК НАЖАТ!] Угол: %.1f° | Наклон: %.1f° | Отправка...\n", 
                      lastKnownAngle, lastKnownPitch);
        
        esp_now_msg_t msg;
        msg.triggerCode = KNOWN_TRIGGER_CODE;
        msg.angle = lastKnownAngle;
        msg.pitch = lastKnownPitch;
        
        // Отправляем пакет дважды подряд без задержек для максимальной надежности при broadcast
        esp_now_send(broadcastAddress, (uint8_t *) &msg, sizeof(msg));
        esp_now_send(broadcastAddress, (uint8_t *) &msg, sizeof(msg));
        
        Serial.printf("[ESP-NOW] Пакет (Угол %.1f°, Наклон %.1f°) отправлен!\n", lastKnownAngle, lastKnownPitch);
    }
    
    // Максимально быстрый цикл, yield() чтобы не падал watchdog WiFi
    yield();
}
