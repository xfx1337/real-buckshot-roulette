#include <ESP8266WiFi.h>
#include <espnow.h>
#include "config.h"
#include <Wire.h>
#include <Adafruit_QMC5883P.h>

Adafruit_QMC5883P mag = Adafruit_QMC5883P();

#undef TRIGGER_PIN
#define TRIGGER_PIN D5 // GPIO14 на Wemos D1 Mini

typedef struct {
    uint32_t triggerCode;
    float angle; // 3D tilt-compensated azimuth (0..360°)
    float pitch; // Наклон ствола в градусах (-90°..+90°)
} __attribute__((packed)) esp_now_msg_t;

uint8_t broadcastAddress[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

void OnDataSent(uint8_t *mac_addr, uint8_t status) {
    Serial.print("[ESP-NOW] Статус отправки: ");
    Serial.println(status == 0 ? "УСПЕШНО" : "ОШИБКА ДОСТАВКИ");
}

bool readMPU6050(float &ax, float &ay, float &az) {
    Wire.beginTransmission(0x68);
    Wire.write(0x3B);
    if (Wire.endTransmission(false) == 0 && Wire.requestFrom(0x68, 6) == 6) {
        int16_t rawX = (int16_t)((Wire.read() << 8) | Wire.read());
        int16_t rawY = (int16_t)((Wire.read() << 8) | Wire.read());
        int16_t rawZ = (int16_t)((Wire.read() << 8) | Wire.read());
        ax = (float)rawX / 16384.0f;
        ay = (float)rawY / 16384.0f;
        az = (float)rawZ / 16384.0f;
        return true;
    }
    return false;
}

void setup() {
    Serial.begin(115200);
    Wire.begin();
    
    // 1. Пробуждаем MPU6050 (0x68) и включаем I2C Bypass
    Wire.beginTransmission(0x68);
    Wire.write(0x6B); // PWR_MGMT_1
    Wire.write(0x00); // Wake up
    Wire.endTransmission();

    Wire.beginTransmission(0x68);
    Wire.write(0x37); // INT_PIN_CFG
    Wire.write(0x02); // Enable I2C Bypass
    Wire.endTransmission();
    delay(100);

    // 2. Инициализируем Adafruit_QMC5883P
    if (!mag.begin()) {
        Serial.println("[КОМПАС] Adafruit_QMC5883P не найден! Проверьте провода.");
    } else {
        mag.setMode(QMC5883P_MODE_NORMAL);
        mag.setODR(QMC5883P_ODR_50HZ);
        Serial.println("[КОМПАС] Adafruit_QMC5883P инициализирован УСПЕШНО.");
    }

    pinMode(TRIGGER_PIN, INPUT_PULLUP);
    
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

// ==== ВСТАВЬ СЮДА ЗНАЧЕНИЯ ИЗ СКЕТЧА КАЛИБРОВКИ ====
float MAG_OFFSET_X = 0.095;
float MAG_OFFSET_Y = 0.093;
float MAG_OFFSET_Z = -0.203;
float MAG_SCALE_X = 0.849;
float MAG_SCALE_Y = 1.197;
float MAG_SCALE_Z = 1.014;
// ===================================================

float lastKnownAngle = 0.0f;
float lastKnownPitch = 0.0f;
float rollRad = 0.0f;
float pitchRad = 0.0f;
unsigned long lastLogMs = 0;

// Глобальные переменные для отладки осей (добавь их рядом с lastKnownAngle)
static float angleXY = 0;
static float angleXZ = 0;
static float angleYZ = 0;
static float lastXCal = 0;
static float lastYCal = 0;
static float lastZCal = 0;

static bool isCalibrating = false;
static unsigned long calibStartMs = 0;
static float calMinX = 9999, calMaxX = -9999;
static float calMinY = 9999, calMaxY = -9999;
static float calMinZ = 9999, calMaxZ = -9999;

void loop() {
    float ax = 0, ay = 0, az = 1.0f;
    bool hasAccel = readMPU6050(ax, ay, az);

    if (hasAccel) {
        // Углы наклона в радианах для компенсации компаса
        rollRad = atan2(ay, az);
        pitchRad = atan2(-ax, sqrt(ay * ay + az * az));

        // Вычисляем pitch в градусах для сервера (какой из них больше, тот и берем, в зависимости от ориентации MPU)
        float pitchX_deg = pitchRad * 180.0f / PI;
        float pitchY_deg = atan2(ay, sqrt(ax * ax + az * az)) * 180.0f / PI;
        lastKnownPitch = (abs(pitchX_deg) > abs(pitchY_deg)) ? pitchX_deg : pitchY_deg;
    }

    if (mag.isDataReady()) {
        float x, y, z;
        if (mag.getGaussField(&x, &y, &z)) {
            
            if (isCalibrating) {
                // Собираем мин/макс для калибровки
                if (x < calMinX) calMinX = x;
                if (x > calMaxX) calMaxX = x;
                if (y < calMinY) calMinY = y;
                if (y > calMaxY) calMaxY = y;
                if (z < calMinZ) calMinZ = z;
                if (z > calMaxZ) calMaxZ = z;

                if (millis() - calibStartMs > 15000) { // 15 секунд калибровки
                    isCalibrating = false;
                    
                    float offsetX = (calMaxX + calMinX) / 2.0;
                    float offsetY = (calMaxY + calMinY) / 2.0;
                    float offsetZ = (calMaxZ + calMinZ) / 2.0;
                    
                    float scaleX = (calMaxX - calMinX) / 2.0;
                    float scaleY = (calMaxY - calMinY) / 2.0;
                    float scaleZ = (calMaxZ - calMinZ) / 2.0;
                    
                    if (scaleX == 0) scaleX = 1; if (scaleY == 0) scaleY = 1; if (scaleZ == 0) scaleZ = 1;
                    float avgScale = (scaleX + scaleY + scaleZ) / 3.0;
                    scaleX = avgScale / scaleX; scaleY = avgScale / scaleY; scaleZ = avgScale / scaleZ;

                    Serial.println("\n=== КАЛИБРОВКА ЗАВЕРШЕНА! ВСТАВЬ ЭТО В КОД ===");
                    Serial.printf("float MAG_OFFSET_X = %.3f;\n", offsetX);
                    Serial.printf("float MAG_OFFSET_Y = %.3f;\n", offsetY);
                    Serial.printf("float MAG_OFFSET_Z = %.3f;\n", offsetZ);
                    Serial.printf("float MAG_SCALE_X = %.3f;\n", scaleX);
                    Serial.printf("float MAG_SCALE_Y = %.3f;\n", scaleY);
                    Serial.printf("float MAG_SCALE_Z = %.3f;\n", scaleZ);
                    Serial.println("==============================================\n");

                    // Применяем сразу, чтобы не надо было перезагружать для проверки
                    MAG_OFFSET_X = offsetX; MAG_OFFSET_Y = offsetY; MAG_OFFSET_Z = offsetZ;
                    MAG_SCALE_X = scaleX; MAG_SCALE_Y = scaleY; MAG_SCALE_Z = scaleZ;
                }
            } else {
                // Обычный рабочий режим
                lastXCal = (x - MAG_OFFSET_X) * MAG_SCALE_X;
                lastYCal = (y - MAG_OFFSET_Y) * MAG_SCALE_Y;
                lastZCal = (z - MAG_OFFSET_Z) * MAG_SCALE_Z;

                // Поскольку Z не меняется (вертикальная ось), мы точно знаем, что плоскость компаса = XY
                angleXY = atan2(lastYCal, lastXCal) * 180.0f / PI;
                if (angleXY < 0) angleXY += 360.0f;
                lastKnownAngle = angleXY; 
            }
        }
    }

    if (!isCalibrating && (millis() - lastLogMs > 1500)) {
        lastLogMs = millis();
        Serial.printf("[СЕНСОРЫ] Наклон: %.1f° | Угол XY:%.0f° | Оси: X:%.2f Y:%.2f Z:%.2f\n",
                      lastKnownPitch, lastKnownAngle, lastXCal, lastYCal, lastZCal);
    } else if (isCalibrating && (millis() - lastLogMs > 200)) {
        lastLogMs = millis();
        Serial.println(">>> КАЛИБРОВКА: КРУТИ ДРОБОВИК ВО ВСЕ СТОРОНЫ! <<<");
    }

    // 2. Опрос кнопки D5 + Сброс удержанием 3 сек
    static bool lastState = HIGH;
    static unsigned long triggerLowStartMs = 0;
    bool state = digitalRead(TRIGGER_PIN);

    if (state == LOW) {
        if (triggerLowStartMs == 0) {
            triggerLowStartMs = millis();
        } else if (millis() - triggerLowStartMs > 3000) {
            Serial.println("\n>>> НАЧАТА КАЛИБРОВКА! (15 секунд) <<<");
            isCalibrating = true;
            calibStartMs = millis();
            calMinX = 9999; calMaxX = -9999;
            calMinY = 9999; calMaxY = -9999;
            calMinZ = 9999; calMaxZ = -9999;
            triggerLowStartMs = millis() + 20000; // Ждем долго до следующего срабатывания
        }
    } else {
        triggerLowStartMs = 0;
    }

    if (state != lastState) {
        Serial.printf("[КНОПКА D5] Изменение состояния: %s -> %s\n", 
                      lastState == HIGH ? "HIGH" : "LOW", 
                      state == HIGH ? "HIGH" : "LOW");
    }

    if (state == LOW && lastState == HIGH) {
        delay(50); // Антидребезг
        if (digitalRead(TRIGGER_PIN) == LOW) {
            Serial.printf(">>> [КУРОК НАЖАТ!] Угол: %.1f° | Наклон: %.1f° | Отправка...\n", 
                          lastKnownAngle, lastKnownPitch);
            
            esp_now_msg_t msg;
            msg.triggerCode = KNOWN_TRIGGER_CODE;
            msg.angle = lastKnownAngle;
            msg.pitch = lastKnownPitch;
            
            for(int i = 0; i < 3; i++) {
                int res = esp_now_send(broadcastAddress, (uint8_t *) &msg, sizeof(msg));
                if (res != 0) {
                    Serial.printf("[ESP-NOW] Ошибка esp_now_send: %d\n", res);
                }
                delay(5);
            }
            Serial.printf("[ESP-NOW] Пакет (Угол %.1f°, Наклон %.1f°) отправлен!\n", lastKnownAngle, lastKnownPitch);
            delay(CFG_TRIGGER_DEBOUNCE_MS);
        } else {
            Serial.println("[КНОПКА D5] Дребезг контактов.");
        }
    }
    lastState = state;
    delay(10);
}
