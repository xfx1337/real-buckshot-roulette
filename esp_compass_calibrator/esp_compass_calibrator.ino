#include <Wire.h>
#include <ESP8266WiFi.h>
#include <Adafruit_QMC5883P.h> // Твоя библиотека из основного кода

Adafruit_QMC5883P mag = Adafruit_QMC5883P();

float xMin = 9999.0, xMax = -9999.0;
float yMin = 9999.0, yMax = -9999.0;
float zMin = 9999.0, zMax = -9999.0;

void setup() {
    Serial.begin(115200);
    Wire.begin();
    
    // ВАЖНО: Пробуждаем MPU6050 и включаем I2C Bypass, как в основном коде.
    // Без этого QMC5883L может быть недоступен, если он подключен через MPU.
    Wire.beginTransmission(0x68);
    Wire.write(0x6B); // PWR_MGMT_1
    Wire.write(0x00); // Wake up
    Wire.endTransmission();
    delay(50);
    
    Wire.beginTransmission(0x68);
    Wire.write(0x37); // INT_PIN_CFG
    Wire.write(0x02); // Enable I2C Bypass
    Wire.endTransmission();
    delay(100);

    if (!mag.begin()) {
        Serial.println("\n[ОШИБКА] QMC5883L не найден! Проверь провода.");
        while (1) { delay(100); } // delay() предотвращает WDT Reset!
    }
    
    mag.setMode(QMC5883P_MODE_NORMAL);
    mag.setODR(QMC5883P_ODR_50HZ);

    Serial.println("\n=============================================");
    Serial.println("      КАЛИБРОВКА КОМПАСА (БЕЗ ВЫЛЕТОВ)       ");
    Serial.println("=============================================");
    Serial.println("1. Начни медленно вращать дробовик в воздухе.");
    Serial.println("2. Крути его 'восьмеркой', наклоняй вверх/вниз.");
    Serial.println("3. Делай это в течение 30-40 секунд.");
    Serial.println("4. Ниже будут обновляться итоговые значения,");
    Serial.println("   которые нужно будет вставить в esp_transmitter.ino.");
    Serial.println("=============================================\n");
    delay(3000);
}

unsigned long lastPrintTime = 0;

void loop() {
    if (mag.isDataReady()) {
        float x, y, z;
        if (mag.getGaussField(&x, &y, &z)) {
            // Игнорируем лютые выбросы
            if (abs(x) < 10.0 && abs(y) < 10.0 && abs(z) < 10.0) {
                bool changed = false;
                
                if (x < xMin) { xMin = x; changed = true; }
                if (x > xMax) { xMax = x; changed = true; }
                if (y < yMin) { yMin = y; changed = true; }
                if (y > yMax) { yMax = y; changed = true; }
                if (z < zMin) { zMin = z; changed = true; }
                if (z > zMax) { zMax = z; changed = true; }

                if (changed && (millis() - lastPrintTime > 300)) {
                    lastPrintTime = millis();
                    
                    float offsetX = (xMax + xMin) / 2.0;
                    float offsetY = (yMax + yMin) / 2.0;
                    float offsetZ = (zMax + zMin) / 2.0;
                    
                    float scaleX = (xMax - xMin) / 2.0;
                    float scaleY = (yMax - yMin) / 2.0;
                    float scaleZ = (zMax - zMin) / 2.0;

                    // Защита от деления на ноль, если масштаб вдруг 0
                    if (scaleX == 0) scaleX = 1;
                    if (scaleY == 0) scaleY = 1;
                    if (scaleZ == 0) scaleZ = 1;

                    // Нормализуем масштаб относительно средних значений
                    float avgScale = (scaleX + scaleY + scaleZ) / 3.0;
                    scaleX = avgScale / scaleX;
                    scaleY = avgScale / scaleY;
                    scaleZ = avgScale / scaleZ;

                    Serial.println("\n--- ВСТАВЬ ЭТО В ESP_TRANSMITTER.INO ---");
                    Serial.print("const float MAG_OFFSET_X = "); Serial.print(offsetX, 3); Serial.println(";");
                    Serial.print("const float MAG_OFFSET_Y = "); Serial.print(offsetY, 3); Serial.println(";");
                    Serial.print("const float MAG_OFFSET_Z = "); Serial.print(offsetZ, 3); Serial.println(";");
                    
                    Serial.print("const float MAG_SCALE_X = "); Serial.print(scaleX, 3); Serial.println(";");
                    Serial.print("const float MAG_SCALE_Y = "); Serial.print(scaleY, 3); Serial.println(";");
                    Serial.print("const float MAG_SCALE_Z = "); Serial.print(scaleZ, 3); Serial.println(";");
                }
            }
        }
    }
    
    // ВАЖНО: эта задержка не дает срабатывать Watchdog Timer (WDT)
    // из-за которого сыпались ошибки '3fffffb0: ...'
    delay(10); 
}
