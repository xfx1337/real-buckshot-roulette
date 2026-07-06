/**
 * Buckshot Roulette IRL — RF 433 MHz Code Scanner (Сканер кодов)
 * 
 * Этот скетч предназначен для считывания кодов радиопередатчика вашего дробовика.
 * Он использует библиотеку RC-Switch.
 * 
 * Инструкция:
 * 1. Прошейте этот скетч в ESP32.
 * 2. Подключите DATA-выход приемника SYN480R к RECEIVER_PIN (GPIO 14).
 * 3. Откройте Serial Monitor в Arduino IDE (скорость 115200).
 * 4. Нажмите на курок дробовика (произведите выстрел).
 * 5. В Serial Monitor отобразится десятичный код (например: "Received 123456 / 24bit Protocol: 1").
 * 6. Скопируйте этот код и запишите его в основной скетч esp32_shotgun.ino в переменную `shot_rf_code`.
 * 
 * Зависимости:
 * - Установите библиотеку "RC-Switch" через Arduino Library Manager.
 */

#include <RCSwitch.h>

#define RECEIVER_PIN 14  // DATA пин приемника SYN480R (GPIO14)

RCSwitch mySwitch = RCSwitch();

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n--- BUCKSHOT RF 433MHz SCANNER START ---");
  Serial.print("Listening on GPIO pin: ");
  Serial.println(RECEIVER_PIN);
  
  // Инициализация прерывания для пина приемника
  mySwitch.enableReceive(digitalPinToInterrupt(RECEIVER_PIN));
  Serial.println("Ready. Press the shotgun trigger to send signal...");
}

void loop() {
  if (mySwitch.available()) {
    
    // Считываем принятые данные
    unsigned long receivedValue = mySwitch.getReceivedValue();
    unsigned int bitLength = mySwitch.getReceivedBitlength();
    unsigned int delayValue = mySwitch.getReceivedDelay();
    unsigned int protocol = mySwitch.getReceivedProtocol();
    
    if (receivedValue == 0) {
      Serial.print("Unknown encoding / Unknown protocol");
    } else {
      Serial.println("=============================================");
      Serial.print(">>> RECEIVED CODE: ");
      Serial.println(receivedValue); // Это число нужно скопировать в основной скетч!
      
      Serial.print("Bit length: ");
      Serial.print(bitLength);
      Serial.println(" bit");
      
      Serial.print("Pulse delay: ");
      Serial.print(delayValue);
      Serial.println(" microseconds");
      
      Serial.print("Protocol: ");
      Serial.println(protocol);
      Serial.println("=============================================\n");
    }
    
    // Сбрасываем флаг приема для готовности к следующему сигналу
    mySwitch.resetAvailable();
  }
}
