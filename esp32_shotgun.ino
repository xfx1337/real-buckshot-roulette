/**
 * Buckshot Roulette IRL — ESP32 Shotgun Controller (с веб-интерфейсом настроек)
 * 
 * Этот скетч предназначен для ESP32.
 * При старте он пытается подключиться к сохраненной WiFi сети.
 * Если подключиться не удалось (или настройки пустые) в течение 15 секунд:
 *   1. ESP32 создает собственную точку доступа WiFi: "Buckshot-Shotgun-Setup"
 *   2. Поднимите телефон, подключитесь к ней, и автоматически откроется страница настроек (Captive Portal).
 *   3. Введите SSID, пароль WiFi и IP/URL сервера дилера. Настройки сохранятся в энергонезависимую память (Preferences).
 * 
 * Скетч использует только встроенные библиотеки ядра ESP32 (дополнительно ничего устанавливать не нужно!).
 */

#include <WiFi.h>
#include <DNSServer.h>
#include <WebServer.h>
#include <Preferences.h>
#include <HTTPClient.h>

// ================= НАСТРОЙКИ ПОДКЛЮЧЕНИЯ =================
// Эти переменные теперь считываются из энергонезависимой памяти (Preferences)
String wifi_ssid = "";
String wifi_pass = "";
String server_url = ""; // Например: http://192.168.1.100:8000

// Настройки Точки Доступа для конфигурации
const char* ap_ssid = "Buckshot-Shotgun-Setup";
const byte DNS_PORT = 53;
IPAddress apIP(192, 168, 4, 1);
DNSServer dnsServer;
WebServer webServer(80);
Preferences preferences;

// ================= НАСТРОЙКИ ПИНОВ =================
#define RECEIVER_PIN     14  // DATA пин от SYN480R (GPIO14)
#define MOSFET_PIN       27  // GATE пин транзистора LR7843 (GPIO27)

// ================= ПАРАМЕТРЫ РАБОТЫ =================
#define POLL_INTERVAL_MS 500   // Интервал опроса сервера о следующем патроне
#define FEEDBACK_MS      150   // Длительность импульса для соленоида (мс). Для автомобильного актуатора замка ставьте 100-150мс, иначе он сгорит!
#define DEBOUNCE_MS      2000  // Защита от повторных срабатываний выстрела (мс)

// Способ детектирования выстрела:
// true  - Использовать библиотеку RC-Switch для кодированных сигналов (требует установки библиотеки RC-Switch)
// false - Использовать прямое чтение уровня (HIGH на RECEIVER_PIN при получении несущей)
#define USE_RC_SWITCH    false 

#if USE_RC_SWITCH
#include <RCSwitch.h>
RCSwitch mySwitch = RCSwitch();
const unsigned long shot_rf_code = 123456; // Код, который отправляет дробовик при спуске
#endif

// ================= ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ =================
enum ShellState {
  SHELL_EMPTY,
  SHELL_BLANK,
  SHELL_LIVE
};

volatile ShellState next_shell = SHELL_EMPTY;
unsigned long last_poll_time = 0;
unsigned long last_shot_time = 0;
bool wifi_connected = false;
bool config_mode = false;

// HTML-код страницы настроек
const char HTML_CONTENT[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html>
<head>
  <meta charset='UTF-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>SHOTGUN SETUP</title>
  <style>
    body { background: #0c0c0c; color: #a0a0a0; font-family: monospace; padding: 20px; text-align: center; }
    h1 { color: #c45050; font-size: 1.5rem; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 5px; }
    h3 { color: #555; font-size: 0.9rem; letter-spacing: 1px; margin-bottom: 25px; }
    .card { background: #111; border: 1px solid #c45050; padding: 24px; max-width: 400px; margin: 20px auto; text-align: left; box-shadow: 0 0 15px rgba(196,80,80,0.2); }
    input[type=text], input[type=password] { width: 100%; padding: 10px; background: #000; border: 1px solid #333; color: #7ca668; font-family: monospace; box-sizing: border-box; margin-bottom: 15px; outline: none; font-size: 0.95rem; }
    input[type=text]:focus, input[type=password]:focus { border-color: #7ca668; }
    input[type=submit] { background: #181818; border: 1px solid #7ca668; color: #7ca668; padding: 12px 20px; width: 100%; cursor: pointer; text-transform: uppercase; font-family: monospace; font-weight: bold; font-size: 0.9rem; transition: background 0.2s; }
    input[type=submit]:hover { background: #2e4425; color: #7ca668; }
    label { font-size: 0.75rem; text-transform: uppercase; color: #888; display: block; margin-bottom: 6px; font-weight: bold; }
  </style>
</head>
<body>
  <h1>BUCKSHOT ROUTLETTE</h1>
  <h3>PHYSICAL SHOTGUN CONFIG</h3>
  <div class='card'>
    <form method='POST' action='/save'>
      <label>WiFi SSID</label>
      <input type='text' name='ssid' value='{SSID}' placeholder='Имя вашей домашней сети'>
      <label>WiFi Password</label>
      <input type='password' name='pass' value='{PASS}' placeholder='Пароль от WiFi'>
      <label>Dealer Server URL (без слэша на конце)</label>
      <input type='text' name='server' value='{SERVER}' placeholder='http://192.168.1.100:8000'>
      <input type='submit' value='SAVE AND REBOOT'>
    </form>
  </div>
</body>
</html>
)rawliteral";

// Прототипы функций
void checkWiFi();
void fetchNextShell();
void triggerShotEvent();
void handleShotDetected();
void startConfigMode();
void loadSettings();
void handleRoot();
void handleSave();
void handleRedirect();

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n--- BUCKSHOT SHOTGUN START ---");
  
  pinMode(MOSFET_PIN, OUTPUT);
  digitalWrite(MOSFET_PIN, LOW);

  // Загружаем настройки из памяти
  loadSettings();

  #if USE_RC_SWITCH
    Serial.println("[RF] Инициализация RC-Switch...");
    mySwitch.enableReceive(digitalPinToInterrupt(RECEIVER_PIN));
  #else
    Serial.println("[RF] Инициализация прямого чтения пина...");
    pinMode(RECEIVER_PIN, INPUT);
  #endif

  // Если имя сети сохранено, пробуем подключиться
  if (wifi_ssid.length() > 0) {
    WiFi.begin(wifi_ssid.c_str(), wifi_pass.c_str());
    Serial.print("[WiFi] Подключение к ");
    Serial.println(wifi_ssid);
    
    // Даем 15 секунд на подключение
    int retries = 0;
    while (WiFi.status() != WL_CONNECTED && retries < 30) {
      delay(500);
      Serial.print(".");
      retries++;
    }
    
    if (WiFi.status() == WL_CONNECTED) {
      Serial.println("\n[WiFi] Успешно подключено!");
      Serial.print("[WiFi] IP-адрес: ");
      Serial.println(WiFi.localIP());
      wifi_connected = true;
    } else {
      Serial.println("\n[WiFi] Не удалось подключиться. Переход в режим настроек.");
      startConfigMode();
    }
  } else {
    Serial.println("[WiFi] Нет сохраненных настроек сети. Вход в режим конфигурации.");
    startConfigMode();
  }
}

void loop() {
  if (config_mode) {
    dnsServer.processNextRequest();
    webServer.handleClient();
    // Быстро мигаем светодиодом (если встроенный) или просто ждем действий
    delay(10);
    return;
  }

  checkWiFi();

  // Регулярный опрос сервера о текущем патроне в патроннике
  if (wifi_connected && (millis() - last_poll_time >= POLL_INTERVAL_MS)) {
    fetchNextShell();
    last_poll_time = millis();
  }

  // Проверка сигнала выстрела
  bool shot_triggered = false;

  #if USE_RC_SWITCH
    if (mySwitch.available()) {
      unsigned long received_value = mySwitch.getReceivedValue();
      if (received_value == shot_rf_code) {
        shot_triggered = true;
      }
      mySwitch.resetAvailable();
    }
  #else
    // Примитивный детектор: если на пине SYN480R появился HIGH уровень
    if (digitalRead(RECEIVER_PIN) == HIGH) {
      shot_triggered = true;
    }
  #endif

  // Если выстрел зафиксирован и прошел таймаут антидребезга
  if (shot_triggered && (millis() - last_shot_time >= DEBOUNCE_MS)) {
    handleShotDetected();
    last_shot_time = millis();
  }
}

/**
 * Загрузка сохраненных параметров из flash-памяти ESP32
 */
void loadSettings() {
  preferences.begin("shotgun-cfg", true);
  wifi_ssid = preferences.getString("ssid", "");
  wifi_pass = preferences.getString("pass", "");
  server_url = preferences.getString("server", "");
  preferences.end();

  Serial.println("[STORAGE] Настройки загружены:");
  Serial.print("  SSID: "); Serial.println(wifi_ssid);
  Serial.print("  SERVER: "); Serial.println(server_url);
}

/**
 * Запуск точки доступа для настройки
 */
void startConfigMode() {
  config_mode = true;
  WiFi.disconnect();
  delay(100);
  
  WiFi.mode(WIFI_AP);
  WiFi.softAPConfig(apIP, apIP, IPAddress(255, 255, 255, 0));
  WiFi.softAP(ap_ssid);
  
  delay(500); // Даем время передатчику запуститься
  
  Serial.println("[AP] Точка доступа запущена!");
  Serial.print("[AP] Подключитесь к сети: "); Serial.println(ap_ssid);
  Serial.print("[AP] URL страницы настроек: http://"); Serial.println(apIP);

  // Запуск DNS-сервера для Captive Portal (перенаправляет все запросы на наш IP)
  dnsServer.start(DNS_PORT, "*", apIP);

  // Маршруты веб-сервера
  webServer.on("/", handleRoot);
  webServer.on("/save", HTTP_POST, handleSave);
  webServer.onNotFound(handleRedirect);
  
  webServer.begin();
  Serial.println("[HTTP] Сервер конфигурации запущен");
}

/**
 * Главная страница конфигурации
 */
void handleRoot() {
  String html = String(HTML_CONTENT);
  html.replace("{SSID}", wifi_ssid);
  html.replace("{PASS}", wifi_pass);
  html.replace("{SERVER}", server_url);
  webServer.send(200, "text/html", html);
}

/**
 * Сохранение настроек в память
 */
void handleSave() {
  String req_ssid = webServer.arg("ssid");
  String req_pass = webServer.arg("pass");
  String req_server = webServer.arg("server");

  // Удаляем пробелы на концах адреса сервера
  req_server.trim();

  preferences.begin("shotgun-cfg", false);
  preferences.putString("ssid", req_ssid);
  preferences.putString("pass", req_pass);
  preferences.putString("server", req_server);
  preferences.end();

  Serial.println("[STORAGE] Новые настройки сохранены:");
  Serial.print("  SSID: "); Serial.println(req_ssid);
  Serial.print("  SERVER: "); Serial.println(req_server);

  String success = "<!DOCTYPE html><html><head><meta charset='UTF-8'><style>body{background:#0c0c0c;color:#7ca668;font-family:monospace;text-align:center;padding:50px;}h2{letter-spacing:2px;text-transform:uppercase;}</style></head><body><h2>CONFIG SAVED</h2><p>Rebooting ESP32... Connect to your WiFi network.</p></body></html>";
  webServer.send(200, "text/html", success);
  
  delay(2000);
  ESP.restart();
}

/**
 * Перенаправление запросов для Captive Portal
 */
void handleRedirect() {
  webServer.sendHeader("Location", "http://192.168.4.1/", true);
  webServer.send(302, "text/plain", "");
}

/**
 * Проверка статуса WiFi и переподключение при необходимости
 */
void checkWiFi() {
  if (WiFi.status() != WL_CONNECTED) {
    if (wifi_connected) {
      Serial.println("[WiFi] Связь потеряна!");
      wifi_connected = false;
    }
    static unsigned long last_reconnect_attempt = 0;
    if (millis() - last_reconnect_attempt > 10000) {
      Serial.println("[WiFi] Попытка переподключения...");
      WiFi.disconnect();
      WiFi.reconnect();
      last_reconnect_attempt = millis();
    }
  } else {
    if (!wifi_connected) {
      Serial.println("[WiFi] Связь восстановлена!");
      wifi_connected = true;
    }
  }
}

/**
 * Запрос типа следующего патрона с сервера
 */
void fetchNextShell() {
  if (server_url.length() == 0) return;

  HTTPClient http;
  String url = server_url + "/api/esp/next_shell";
  
  http.begin(url);
  http.setTimeout(1500); 
  
  int httpCode = http.GET();
  
  if (httpCode == HTTP_CODE_OK) {
    String payload = http.getString();
    
    if (payload.indexOf("\"live\"") != -1) {
      if (next_shell != SHELL_LIVE) {
        next_shell = SHELL_LIVE;
        Serial.println("[GAME] Следующий патрон: БОЕВОЙ");
      }
    } else if (payload.indexOf("\"blank\"") != -1) {
      if (next_shell != SHELL_BLANK) {
        next_shell = SHELL_BLANK;
        Serial.println("[GAME] Следующий патрон: ХОЛОСТОЙ");
      }
    } else {
      if (next_shell != SHELL_EMPTY) {
        next_shell = SHELL_EMPTY;
        Serial.println("[GAME] Ствол пуст или игра не активна");
      }
    }
  } else {
    Serial.print("[HTTP] Ошибка запроса патрона: ");
    Serial.println(httpCode);
  }
  http.end();
}

/**
 * Обработка зафиксированного выстрела
 */
void handleShotDetected() {
  Serial.println("\n====================================");
  Serial.println("[SHOT] ЗАФИКСИРОВАН ВЫСТРЕЛ!");
  
  if (next_shell == SHELL_LIVE) {
    Serial.println("[ACTION] Патрон боевой! Активация MOSFET...");
    digitalWrite(MOSFET_PIN, HIGH);
    
    triggerShotEvent();
    
    delay(FEEDBACK_MS);
    digitalWrite(MOSFET_PIN, LOW);
    Serial.println("[ACTION] MOSFET отключен");
  } 
  else if (next_shell == SHELL_BLANK) {
    Serial.println("[ACTION] Патрон холостой. MOSFET не активирован.");
    triggerShotEvent();
  } 
  else {
    Serial.println("[ACTION] Выстрел вхолостую (магазин пуст). Игнорируем физический удар.");
    triggerShotEvent();
  }
  Serial.println("====================================\n");
}

/**
 * Отправка сообщения о выстреле на сервер
 */
void triggerShotEvent() {
  if (!wifi_connected || server_url.length() == 0) {
    Serial.println("[HTTP] Нет WiFi или не задан сервер, отправка события невозможна!");
    return;
  }

  HTTPClient http;
  String url = server_url + "/api/esp/shot_fired";
  
  Serial.println("[HTTP] Отправка события выстрела на сервер...");
  http.begin(url);
  http.setTimeout(2000);
  
  int httpCode = http.GET();
  
  if (httpCode == HTTP_CODE_OK) {
    Serial.println("[HTTP] Сервер подтвердил регистрацию выстрела!");
    next_shell = SHELL_EMPTY;
  } else {
    Serial.print("[HTTP] Ошибка отправки выстрела: ");
    Serial.println(httpCode);
  }
  http.end();
}
