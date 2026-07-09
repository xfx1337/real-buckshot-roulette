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
#include <esp_task_wdt.h>

// Все настройки (Wi-Fi, адрес сервера, пины, код пульта-курка) берутся из
// config.h, который генерируется из корневого config.json скриптом
// esp/gen_config.py. Перед прошивкой выполни:  python esp/gen_config.py
//
// Чтобы узнать код нового пульта: поставь "code": 0 в config.json, перегенерируй
// config.h, прошей — прошивка в режиме обучения печатает пойманный код в Serial.
// Затем впиши код в config.json, снова перегенерируй и перепрошей.
#include "config.h"

RCSwitch rfTrigger = RCSwitch();

// ── Тайминги и пороги ──
// Все значения приходят из config.json (блок esp.timings) через config.h.
// Меняй их ТАМ и перегенерируй config.h (python esp/gen_config.py) — здесь
// только псевдонимы, чтобы не менять код в десятке мест.
static const unsigned long POLL_INTERVAL_MS = CFG_POLL_INTERVAL_MS;      // фоновый опрос для LED-индикатора
static const unsigned long HTTP_TIMEOUT_MS = CFG_HTTP_TIMEOUT_MS;        // таймаут HTTP; 400мс было мало для первого TCP-хендшейка
static const unsigned long SOLENOID_PULSE_MS = CFG_SOLENOID_PULSE_MS;    // длительность импульса на соленоид (100-200ms)
static const unsigned long SOLENOID_MAX_ON_MS = CFG_SOLENOID_MAX_ON_MS;  // аварийный предел — жёстко выключаем после него
static const unsigned long TRIGGER_DEBOUNCE_MS = CFG_TRIGGER_DEBOUNCE_MS;// минимальный интервал между выстрелами

// Подтверждение курка по ДВУМ валидным пакетам подряд. Один нажим пульта
// физически шлёт пачку одинаковых пакетов с интервалом в единицы-десятки мс.
// Требуем 2 таких пакета в пределах окна — это отсекает ложные ОДИНОЧНЫЕ
// срабатывания от эфирного шума. Всё, что приходит после подтверждения (третий
// и далее пакеты той же пачки) — игнорируем до конца пачки.
static const unsigned int RF_CONFIRM_COUNT = CFG_RF_CONFIRM_COUNT;          // сколько валидных пакетов подряд нужно для выстрела
static const unsigned long RF_CONFIRM_WINDOW_MS = CFG_RF_CONFIRM_WINDOW_MS; // макс. интервал между пакетами одной пачки
// «Глухой» период после подтверждённого выстрела: любые валидные RF-пакеты в
// это окно поглощаются и НЕ считаются — это хвост той же пачки от одного
// нажатия. Гарантирует «одно нажатие = один выстрел». Должен быть длиннее всей
// пачки пульта (обычно 100-500мс) + импульса соленоида.
static const unsigned long RF_LOCKOUT_MS = CFG_RF_LOCKOUT_MS;
static const unsigned long WIFI_RETRY_INTERVAL_MS = CFG_WIFI_RETRY_INTERVAL_MS;  // даём WPA2-хендшейку время завершиться перед повтором
static const unsigned long LIVE_LED_BLINK_MS = CFG_LIVE_LED_BLINK_MS;      // период мигания лампочки D2

// Аппаратный сторож (Task Watchdog Timer). Если loop() завис дольше этого
// таймаута (например, плату подвесила помеха от соленоида на боевом патроне
// или зависший HTTP/Wi-Fi стек) — чип сам делает reboot. Порог с запасом над
// самым долгим ЛЕГАЛЬНЫМ блоком в loop: HTTP таймаут 1.5с + импульс соленоида
// 0.25с. 5с — значит нормальная работа watchdog не заденет, а реальное
// зависание поймает за секунды.
static const uint32_t WDT_TIMEOUT_MS = CFG_WDT_TIMEOUT_MS;

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

// Счётчик валидных RF-пакетов подряд для подтверждения курка (см.
// RF_CONFIRM_COUNT). rfValidStreak растёт, пока пакеты идут в пределах окна;
// lastRfValidMs — время последнего валидного пакета для проверки окна.
unsigned int rfValidStreak = 0;
unsigned long lastRfValidMs = 0;
// Момент последнего подтверждённого выстрела. Пока с него не прошло
// RF_LOCKOUT_MS, весь хвост пачки от того же нажатия поглощается.
unsigned long lastFireMs = 0;
bool rfLockoutActive = false;

// ── Замер задержки «приём курка → соленоид» ──
// firstPacketMicros — момент приёма ПЕРВОГО валидного пакета серии (нажатие
// принято). Используется, чтобы напечатать задержку до момента срабатывания
// соленоида в fireTrigger(). 0 = нет активного замера.
unsigned long firstPacketMicros = 0;

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
        // Команда дилера «принудительно щёлкнуть соленоидом» (кнопка на пульте
        // оператора). Сервер выставляет fire=true разово; отрабатываем импульс
        // сразу, минуя игровую логику боевого/холостого. Вызывается из loop()
        // (не из прерывания), поэтому delay() тут безопасен.
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
        // СНАЧАЛА дёргаем соленоид — это самый горячий путь, никакого Serial
        // перед ним быть не должно (печать строки на 115200 = ~5-10мс задержки).
        setSolenoid(true);
        // Замер снимаем сразу после включения — момент физического удара.
        unsigned long latencyUs = (firstPacketMicros != 0) ? micros() - firstPacketMicros : 0;
        // Держим импульс. Serial-логи печатаем ВНУТРИ паузы: удержание всё равно
        // ждёт delay(), так что вывод не добавляет задержки к самому спуску.
        Serial.println(">>> Курок: боевой патрон — импульс на соленоид!");
        if (latencyUs) {
            Serial.print("[ЗАМЕР] Задержка курок→соленоид: ");
            Serial.print(latencyUs / 1000.0, 1);
            Serial.println(" мс");
        }
        delay(SOLENOID_PULSE_MS);
        setSolenoid(false);
    } else {
        Serial.println(">>> Курок: холостой (или сервер недоступен/не фаза стрельбы) — тишина.");
        if (firstPacketMicros != 0) {
            unsigned long latencyUs = micros() - firstPacketMicros;
            Serial.print("[ЗАМЕР] Задержка курок→решение (холостой): ");
            Serial.print(latencyUs / 1000.0, 1);
            Serial.println(" мс");
        }
    }
    firstPacketMicros = 0;  // замер завершён

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
        unsigned long now = millis();

        // «Глухой» период после выстрела: пока не прошло RF_LOCKOUT_MS с
        // последнего выстрела — это ещё хвост той же пачки от одного нажатия.
        // Молча поглощаем пакет, держим streak на нуле и продлеваем окно, чтобы
        // хвост не набрал новую пару. Так одно нажатие = ровно один выстрел.
        if (rfLockoutActive && now - lastFireMs < RF_LOCKOUT_MS) {
            rfValidStreak = 0;
            lastRfValidMs = now;
            drainRfBuffer();     // разом выкидываем весь накопленный хвост
            return;
        }
        rfLockoutActive = false;

        // Подтверждение по ДВУМ валидным пакетам подряд. Считаем пакеты, пока
        // они идут в пределах окна RF_CONFIRM_WINDOW_MS (одна пачка от одного
        // нажатия). Если пауза больше окна — это новое нажатие, отсчёт с нуля.
        if (now - lastRfValidMs > RF_CONFIRM_WINDOW_MS) {
            rfValidStreak = 0;   // окно истекло — начинаем новую серию
        }
        lastRfValidMs = now;
        rfValidStreak++;
        // Метка времени первого пакета серии — точка отсчёта задержки до соленоида.
        if (rfValidStreak == 1) {
            firstPacketMicros = micros();
        }
        rfTrigger.resetAvailable();

        if (rfValidStreak >= RF_CONFIRM_COUNT) {
            // Набрали нужное число пакетов подряд — стреляем. Включаем lockout,
            // сбрасываем счётчик и выкидываем остаток пачки. Дальнейший хвост
            // поглотит проверка lockout выше.
            rfValidStreak = 0;
            lastFireMs = now;
            rfLockoutActive = true;
            fireTrigger();
            drainRfBuffer();
        }
    } else {
        // Сигнал с чужого пульта — молча выбрасываем этот пакет.
        rfTrigger.resetAvailable();
    }
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

    // Сначала поднимаем Wi-Fi, и только потом включаем RF-приёмник. Иначе
    // прерывания от шумящего приёмника на GPIO4 сыплются во время критичного
    // WPA2-хендшейка и мешают ему завершиться.
    WiFi.mode(WIFI_STA);
    // Разовое сканирование при старте: печатает все видимые сети в Serial.
    // Если целевого SSID тут нет — плата его физически не видит (вне зоны, 5ГГц,
    // выключена точка). Если есть, но подключения нет — дело в пароле/защите.
    scanNetworks();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    Serial.print("[WiFi] Подключаюсь к \"");
    Serial.print(WIFI_SSID);
    Serial.println("\"...");

    // НЕ блокируем setup() ожиданием Wi-Fi. Раньше здесь крутился цикл до 8с,
    // но он (а) длиннее таймаута watchdog (WDT_TIMEOUT_MS, обычно 5с) и (б) не
    // кормил сторож — esp_task_wdt_reset() зовётся только в конце loop(), до
    // которого мы ещё не дошли. Если Wi-Fi не поднимался за 5с (медленная или
    // недоступная локальная сеть без интернета — типовой случай на автономной
    // точке доступа), watchdog ребутил плату ПРЯМО в setup(), и она уходила в
    // вечный цикл перезагрузок, ни разу не дойдя до рабочего loop().
    //
    // Теперь подключение целиком фоновое: ensureWifiConnected() в loop() сам
    // доводит хендшейк до конца и переподключается при обрывах. Плата
    // немедленно входит в loop() и работает (курок/соленоид/LED) независимо от
    // того, есть ли уже сеть. Серверные запросы просто молчат, пока Wi-Fi не
    // поднялся (pollShellStatus/sendShootToServer проверяют WL_CONNECTED сами).
    Serial.println("[WiFi] Подключение идёт в фоне, вхожу в рабочий цикл.");

    rfTrigger.enableReceive(digitalPinToInterrupt(TRIGGER_PIN));
}

void loop() {
    // Курок проверяем ПЕРВЫМ в проходе — раньше всего остального, чтобы между
    // приёмом RF-пакета и импульсом соленоида было минимум задержки. Всё
    // тяжёлое (Wi-Fi, блокирующий HTTP-опрос) идёт ПОСЛЕ.
    handleRfTrigger();

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
        // Опрос — блокирующий (до http_timeout). Пакет курка мог прилететь
        // ПРЯМО во время него: обрабатываем сразу, не дожидаясь нового прохода.
        handleRfTrigger();
    }

    updateLiveLed();

    // Отложенное уведомление сервера о выстреле (HTTP вне обработчика курка).
    if (pendingShoot) {
        pendingShoot = false;
        sendShootToServer();
    }

    // «Кормим» сторож в самом конце прохода: если loop где-то выше завис
    // дольше WDT_TIMEOUT_MS и сюда не дошёл — watchdog перезагрузит плату.
    esp_task_wdt_reset();
}
