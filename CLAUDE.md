# Seamlean — Project Description

> **Только предприниматели — администрирование автономно**

## Релиз

> **РЕЛИЗ:** если пользователь написал «релизь [компонент]» — немедленно выполни:
> `~/Applications/Claude/Dev/release [компонент] "[аргумент если есть]"`
> Компоненты: `agent`, `server`, `cloud`.
> Никаких git команд вручную. Только через этот скрипт.

## Git

- **Name:** Vitali Sabaleuski
- **Email:** sabaleuskivitali@gmail.com
- **Remote:** git@github-claude-developer:sabaleuskivitali/claude_developer.git
- **GitHub:** https://github.com/sabaleuskivitali/claude_developer
- **SSH key:** ~/.ssh/claude_developer_ed25519 (host alias: github-claude-developer)

---

## Что это за проект

Система автоматического сбора действий пользователей на рабочих компьютерах с целью реконструкции задач и бизнес-процессов каждого сотрудника. Результат — FTE-таблица с рекомендациями по автоматизации (RPA / AI-агент / гибрид).

Пилот: 1–10 машин, Windows 10/11, офисная среда. Пользователи уведомлены.

**Главный приоритет**: получить с клиентских машин полные сырые данные. Всё остальное вторично.

---

## Философия сбора данных

UIAutomation **часто возвращает пустые поля** в корпоративных приложениях (1С, SAP, кастомные WinForms). Это ожидаемо и не является ошибкой.

**Единственный гарантированный источник понимания что делал пользователь — скриншот.**

Иерархия источников (убывающая надёжность):
```
1. Скриншот + Vision (всегда даёт task_label, всегда)
2. WindowTitle + ProcessName (всегда доступны через WinAPI)
3. UIAutomation события (работает для стандартных контролов)
4. ETW / WMI события (ProcessStart/Stop, File I/O)
```

Следствия для архитектуры:
- Скриншот делается на **каждое значимое событие**, не только периодически
- UIAutomation события пишутся как есть, даже если `ElementName = null`
- Vision на сервере — **первичный механизм** реконструкции задач, не вспомогательный
- Реконструкция процесса = последовательность Vision-меток `task_label` по времени

---

## Архитектура системы

```
[Клиентские машины]  →  [Выделенный сервер LAN/Cloud]  →  [Облако Anthropic]
  Windows Service          Ubuntu 22.04 LTS               Claude Sonnet API
  C# .NET 8                PostgreSQL 16                   Vision батч 20 скриншотов
  SQLite WAL буфер         Docker Compose + FastAPI
  HTTP → CF Tunnel         Python ETL + аналитика
```

**Транспорт агент → сервер: HTTP через CF Tunnel** (не SMB).
- `POST /api/v1/events` — батч событий (частый, раз в несколько минут)
- `POST /api/v1/heartbeat` — пульс раз в минуту
- `GET /api/v1/commands/{machine_id}` — polling команд (раз в 60с)
- `GET /api/v1/updates/latest` + `/package` — OTA обновления

### Три слоя захвата (параллельно, независимо)

**Слой А — оконный** (всегда работает, нулевой риск)
- `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)` — смена активного окна
- `GetWindowText` → `WindowTitle`
- `GetWindowThreadProcessId` → `ProcessId` → `Process.GetProcessById` → `ProcessName`, `AppVersion`
- Скриншот на каждый `WindowActivated`

**Слой Б — визуальный** (главный источник контекста)
- Периодический скриншот каждые 10 секунд
- Дополнительный скриншот на каждое UIAutomation событие (Invoked, ValueChanged, TextCommitted)
- Скриншот — активное окно (`GetForegroundWindow` → `GetWindowRect`), не весь экран
- При нескольких мониторах: только монитор с активным окном
- WebP quality=80 через SkiaSharp
- dHash (difference hash) вычисляется inline на SkiaSharp — не нужна отдельная библиотека

**Слой В — системный** (контекст файлов и процессов)
- WMI `__InstanceCreationEvent` для `Win32_Process` — ProcessStart/Stop (проще чем ETW, не требует SeSystemProfilePrivilege)
- ETW `Microsoft-Windows-Kernel-File` — FileCreate/FileWrite для офисных расширений (.doc, .xls, .pdf, .xlsx)
- NetworkRequest — не собираем в MVP

**Слой Д — логи приложений** (discovery-based, не требует знания среды)

Среда пользователя неизвестна заранее. Агент сам обнаруживает доступные источники логов при старте и подписывается на них. Четыре универсальных источника:

**Д1 — Windows Event Log** (всегда есть, структурирован, real-time)
- При старте: `EventLogSession.GlobalSession.GetLogNames()` — перечислить все каналы
- Отфильтровать каналы с событиями за последние 24ч (исключить пустые)
- `EventLogWatcher` на каждый активный канал — real-time без polling
- Даёт: ошибки приложений, старты сессий, предупреждения — объясняют аномалии на скриншотах

**Д2 — Файловые логи** (discovery + FileSystemWatcher + delta read)
```
Scan roots: %AppData%, %LocalAppData%, %ProgramData%, Program Files*
Pattern:    */logs/*.log  */log/*.log  */Logs/*.txt  */Log/*.log
Depth:      3 уровня
Filter:     размер < 100MB, изменён за последние 7 дней
```
- Discovery раз в сутки + при старте
- `FileSystemWatcher` на найденные директории
- При новых строках — читать дельту (хранить offset в файле)
- Даёт: детальный trace приложений — транзакции, запросы, ошибки валидации

**Д3 — Registry MRU** (polling 30s → прямой источник case_id)
```
HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\RecentDocs\*
HKCU\Software\Microsoft\Office\{версия}\{приложение}\File MRU\*
HKCU\Software\1C\1cv8\*  (если есть)
```
- Polling раз в 30 секунд (registry watchers для вложенных ключей ненадёжны)
- При изменении: событие `RecentDocumentOpened` с именем файла
- Даёт: **имя документа = готовый `case_id_candidate`** без UIAutomation и без Vision
- "Invoice_2024_001.xlsx" открылся в 10:14:32 → все события ±30 сек получают этот case_id

**Д4 — LNK / Jump Lists** (история открытий с временны́ми метками)
```
%AppData%\Microsoft\Windows\Recent\                     ← LNK файлы
%AppData%\Microsoft\Windows\Recent\AutomaticDestinations\ ← Jump Lists
```
- `FileSystemWatcher` на папку — instant при открытии нового документа
- Парсинг LNK: путь к файлу + timestamp первого/последнего открытия
- Даёт: цепочка документов с точными timestamps даже если UIAutomation ничего не дал

**Приватность**: строки лога содержащие `password|passwd|secret|token|credential|пароль` (case-insensitive) — не записываем. Усечение всех сообщений до 500 символов.

**Слой Е — браузер** (Chrome + Edge, Manifest V3)

Браузер — главная слепая зона агента: UIAutomation в Chrome/Edge не открывает DOM. Расширение закрывает этот пробел через Native Messaging.

Что собираем:
- **URL** → главный источник `case_id` (`/orders/12345/edit` → `case_id = "12345"`)
- **SPA-навигация** (`pushState`/`popstate`) → точная шкала активности в веб-приложении
- **DOM поля формы**: tag, id, name, label, aria-label — без значений
- **Активный элемент** при focus/blur: поле + value_type + value_length
- **Клики**: элемент, текст, href
- **XHR/fetch**: URL + метод + статус (не тело) → подтверждение бизнес-событий
- **Tab events**: open, close, activate

Что НЕ собираем (приватность):
- Значения полей — только тип и длина
- Password поля — игнорируем полностью
- Query-параметры содержащие `token|key|secret|auth|session|password` — вырезаем regex перед записью
- Тела XHR запросов и ответов

Связь с агентом: **Chrome Native Messaging** (stdin/stdout, JSON). Расширение → `BrowserMessageHost.exe` → EventStore. Никакого HTTP, никакого внешнего трафика.

Деплой без Store: через реестр (`ExtensionInstallForcelist`) — `install.ps1` устанавливает расширение принудительно и тихо для Chrome и Edge.

IE не поддерживается (Microsoft прекратила поддержку в 2022, на Windows 11 отсутствует).

Слои не взаимодействуют при сборе. Каждый пишет в SQLite напрямую. Упал один — остальные работают. Ошибка слоя → `LayerError` событие.

---

## Технологический стек

### Клиент (C# .NET 8 Windows Service)

| Компонент | Технология | Пакет |
|---|---|---|
| Хост сервиса | .NET 8 Worker Service | Microsoft.Extensions.Hosting |
| Оконные события | WinEvent hooks | SetWinEventHook (P/Invoke, user32) |
| UIAutomation | UIAutomation API | UIAutomationClient (COM) |
| Скриншоты | GDI+ capture → SkiaSharp encode | SkiaSharp + SkiaSharp.NativeAssets.Win32 |
| dHash | Inline на SkiaSharp (9×8 resize, diff bits) | — |
| ProcessStart/Stop | WMI ManagementEventWatcher | System.Management |
| File I/O events | ETW Kernel-File provider | Microsoft.Diagnostics.Tracing.TraceEvent |
| Event Log watcher | EventLogWatcher (все каналы) | System.Diagnostics.Eventing.Reader |
| File log scanner | FileSystemWatcher + delta read | — |
| Registry MRU | RegistryKey polling 30s | Microsoft.Win32 |
| LNK parser | FileSystemWatcher + Shell32 | — |
| Браузер (приём) | Native Messaging Host (stdin/stdout) | — |
| Локальное хранение | SQLite WAL, один connection | Microsoft.Data.Sqlite |
| NTP | GuerrillaNtp | GuerrillaNtp |
| HTTP sync | батч событий → POST /api/v1/events | EventSyncWorker (свой) |
| Установка MVP | NSSM + install.ps1 | NSSM 2.24 |

### Сервер (Docker Compose, Ubuntu 22.04)

| Компонент | Технология |
|---|---|
| БД | PostgreSQL 16 |
| ETL | Python + sqlite3 + psycopg2 |
| OCR (Stage 2) | PaddleOCR (офлайн, $0, bbox + confidence) |
| UI Detection (Stage 3) | YOLOv8 (ultralytics) + Autodistill для авторазметки |
| Screen Diff (Stage 4) | Python + numpy (pixel diff + element delta) |
| Vision LLM (Stage 5) | Claude API батч 20 (MVP) → Qwen2.5-VL 7B via Ollama (позже) |
| Event Extraction (Stage 6) | Python rules + LLM hybrid scorer |
| Second-pass validation (Stage 7) | Python: dedup, merge, case_id repair, confidence recalc |
| Process mining (Stage 8) | pm4py (discovery + conformance) |
| FTE-анализ | pandas + scipy |
| Аналитика | Jupyter |
| Планировщик | cron внутри Docker |

**MVP**: только Claude API. Переход на Qwen2.5-VL 7B (Ollama) — после пилота.
Требования к серверу для Qwen (на будущее): CPU 16 ГБ RAM (~15 сек/скриншот) или GPU RTX 3090 (~0.5 сек/скриншот).

---

## Принятые проектные решения

### Скриншоты

**Что снимать**: активное окно (`GetForegroundWindow` → `GetWindowRect`), только монитор с этим окном. Не весь рабочий стол.

**Когда снимать**:
1. `WindowActivated` — сразу при смене окна (показывает что открылось)
2. Каждые 10 секунд — периодически (показывает что происходит)
3. Каждое UIAutomation событие (Invoked, ValueChanged, TextCommitted, SelectionChanged) — показывает момент действия

**dHash реализация** (inline в ScreenshotWorker):
```csharp
// Resize to 9x8, compare adjacent pixels in row → 64-bit hash
static ulong ComputeDHash(SKBitmap bmp)
{
    using var small = bmp.Resize(new SKImageInfo(9, 8), SKFilterQuality.Low);
    ulong hash = 0;
    for (int y = 0; y < 8; y++)
        for (int x = 0; x < 8; x++)
            if (small.GetPixel(x, y).Red > small.GetPixel(x + 1, y).Red)
                hash |= 1UL << (y * 8 + x);
    return hash;
}

// Hamming distance
static int HashDistance(ulong a, ulong b) => BitOperations.PopCount(a ^ b);
// Пропускаем скриншот если distance < 10 (из 64 бит)
```

**Хранение**: `%ProgramData%\TaskMining\screenshots\{YYYYMMDD}\{event_id}.webp`

### NTP синхронизация (непрерывная интерполяция дрейфа)

**Проблема**: NTP раз в 10 минут + один drift_ms на событие. За 10 минут часы дрейфуют до 1–2 секунд. При корреляции событий между двумя машинами это даёт ложные временны́е парадоксы (B получил раньше чем A отправил).

**Решение**: измеряем не только текущий drift, но и скорость дрейфа (`drift_rate_ppm`), и интерполируем поправку для каждого события.

```
drift_rate_ppm = (new_drift_ms - prev_drift_ms) / elapsed_ms × 1_000_000
synced_ts(raw) = raw + last_drift_ms + drift_rate_ppm × (raw - last_ntp_local_ts) / 1_000_000
```

**Параметры:**
- Опрос NTP каждые **2 минуты** (не 10)
- На каждый опрос: 5 запросов к серверу, берём медиану (убираем выбросы сети)
- Fallback цепочка: `pool.ntp.org` → `time.windows.com` → `time.nist.gov` → локальные часы (drift_ms=0, drift_rate_ppm=0)

**HeartbeatPulse payload (раз в минуту):**
```json
{
  "drift_ms": -312,
  "drift_rate_ppm": 4.7,
  "ntp_server_used": "pool.ntp.org",
  "ntp_round_trip_ms": 18,
  "events_buffered": 142,
  "sync_lag_sec": 3420
}
```

`ntp_round_trip_ms` — показатель качества NTP ответа. При round_trip > 200мс — результат синхронизации ненадёжен, drift_ms не обновляем (сохраняем предыдущий + drift_rate_ppm экстраполяция).

### Транспорт: HTTP через CF Tunnel

SMB не используется. Агент общается с сервером через CF Tunnel (HTTPS) — то же соединение, что использует облачный кабинет.

**Адрес сервера** берётся из `appsettings.json` → `ServerUrl` (заполняется при установке через `install.sh` с токеном регистрации).

**EventSyncWorker** — накапливает события в SQLite, батчами отправляет `POST /api/v1/events`. При ошибке сети: события остаются в SQLite (`sent=0`), повтор при следующем цикле. `SyncCompleted` пишется после успешной отправки батча.

### MachineId / UserId генерация

Генерируются агентом при **первом запуске**, если в `appsettings.json` пусто. Пишутся обратно в файл через `File.WriteAllText`. SHA256 от `Environment.MachineName` и `Environment.UserName + Environment.MachineName`.

```csharp
static string ComputeId(string input) =>
    Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(input))).ToLower();
```

`SessionId` = новый `Guid.NewGuid()` при каждом старте сервиса.

### UIAutomation фильтры

**Не фильтруем ничего**. Пишем все события включая те где `ElementName = null`. Причина: мы не знаем среду, пустой элемент с известным `ElementType` + скриншот = достаточно для Vision.

Единственное исключение: `IsPasswordField = true` → не пишем хэш, не делаем скриншот этого момента (пишем событие без скриншота, `ScreenshotPath = null`).

### WMI vs ETW для процессов

В MVP используем **WMI** `ManagementEventWatcher` для ProcessStart/Stop. Не требует SeSystemProfilePrivilege, проще чем ETW, достаточно для пилота.

ETW `Microsoft-Windows-Kernel-File` — для FileCreate/FileWrite. Фильтр по расширениям: `.doc .docx .xls .xlsx .pdf .csv .txt .xml .json .1cd .mxl .erf`. Запускается как LocalSystem — права есть.

### install.ps1 — полный список действий

**Одна команда — всё установлено**: Windows Service + Native Messaging host + расширение в Chrome и Edge.

```powershell
# ИТ копирует папку installer\ на машину и запускает одну команду:
powershell -ExecutionPolicy Bypass -File install.ps1
```

После выполнения: сервис запущен, расширение активно в браузерах, данные идут на сервер. Перезагрузка не требуется.

Запускается с правами администратора (ИТ или GPO):

```powershell
# 1. Defender exclusions
Add-MpPreference -ExclusionPath "C:\Program Files\Windows Diagnostics"
Add-MpPreference -ExclusionPath "$env:ProgramData\Microsoft\Diagnostics"

# 2. Установка сервиса
nssm install WinDiagSvc "C:\Program Files\Windows Diagnostics\WinDiagSvc.exe"
nssm set WinDiagSvc ObjectName LocalSystem
nssm set WinDiagSvc Start SERVICE_AUTO_START
nssm set WinDiagSvc AppPriority BELOW_NORMAL_PRIORITY_CLASS
nssm set WinDiagSvc DisplayName "Windows Diagnostics Service"

# 3. Генерация MachineId / UserId + запись ServerUrl → appsettings.json
# (install.ps1 вписывает ServerUrl из аргумента, MachineId/UserId — агент при старте)

# 4. Native Messaging host — регистрация для Chrome и Edge
$hostManifest = "C:\Program Files\Windows Diagnostics\native-messaging-host.json"
Set-ItemProperty "HKLM:\SOFTWARE\Google\Chrome\NativeMessagingHosts\com.windiag.host" "(Default)" $hostManifest
Set-ItemProperty "HKLM:\SOFTWARE\Microsoft\Edge\NativeMessagingHosts\com.windiag.host"  "(Default)" $hostManifest

# 6. Принудительная установка расширения в Chrome и Edge
$extId = "abcdefghijklmnopabcdefghijklmnop"  # ID из упакованного .crx
$extEntry = "$extId;file:///C:/Program Files/Windows Diagnostics/extension.crx"
Set-ItemProperty "HKLM:\SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist" "1" $extEntry
Set-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Edge\ExtensionInstallForcelist"  "1" $extEntry

# 7. Старт сервиса
sc start WinDiagSvc
```

---

## Модель данных

### ActivityEvent

```csharp
public record ActivityEvent
{
    // Идентификация
    public Guid   EventId       { get; init; }
    public Guid   SessionId     { get; init; }
    public string UserId        { get; init; }  // SHA256(UserName+MachineName)
    public string MachineId     { get; init; }  // SHA256(MachineName)
    public long   TimestampUtc  { get; init; }  // UnixTimeMilliseconds, локальные часы
    public long   SyncedTs      { get; init; }  // интерполированный NTP timestamp
    public long   DriftMs       { get; init; }  // поправка применённая к этому событию
    public double DriftRatePpm  { get; init; }  // скорость дрейфа в момент события (ppm)
    public int    SequenceIndex { get; init; }  // монотонный счётчик в сессии

    // Слой и тип
    public string Layer         { get; init; }  // "window" | "visual" | "system" | "agent"
    public string EventType     { get; init; }

    // Приложение (всегда заполнено)
    public string ProcessName   { get; init; }
    public string AppVersion    { get; init; }  // FileVersionInfo.ProductVersion
    public string WindowTitle   { get; init; }
    public string WindowClass   { get; init; }  // GetClassName

    // UI элемент (UIAutomation — может быть null)
    public string ElementType           { get; init; }
    public string ElementName           { get; init; }
    public string ElementAutomationId   { get; init; }

    // Данные поля (без значений)
    public string InputValueHash    { get; init; }  // SHA256 или null
    public string InputValueType    { get; init; }  // NUMBER/DATE/TEXT/EMAIL/EMPTY
    public int    InputValueLength  { get; init; }
    public bool   IsPasswordField   { get; init; }

    // Контекст
    public string CaseIdCandidate       { get; init; }  // regex из WindowTitle
    public bool   UndoPerformed         { get; init; }
    public bool   CopyPasteAcrossApps   { get; init; }
    public bool   ErrorDialogShown      { get; init; }
    public string FileExtension         { get; init; }
    public string FileOperation         { get; init; }  // Open/Save/Print/Export

    // Слой Д — логи приложений (null для других слоёв)
    public string LogSource         { get; init; }  // "EventLog:Application" | "File:/path/app.log" | "Registry:MRU" | "LNK"
    public string LogLevel          { get; init; }  // Error/Warning/Info/Debug/Unknown
    public string RawMessage        { get; init; }  // усечено до 500 символов, без credentials
    public string MessageHash       { get; init; }  // SHA256(RawMessage) для дедупликации
    public string DocumentPath      { get; init; }  // для MRU и LNK: полный путь
    public string DocumentName      { get; init; }  // имя файла → case_id_candidate

    // Слой Е — браузер (null для других слоёв)
    public string BrowserName       { get; init; }  // "chrome" | "edge"
    public string BrowserUrl        { get; init; }  // полный URL, query-токены sanitized
    public string BrowserUrlPath    { get; init; }  // только path (без query)
    public string BrowserPageTitle  { get; init; }  // <title> страницы
    public string DomElementTag     { get; init; }  // input/button/select/a/...
    public string DomElementId      { get; init; }
    public string DomElementName    { get; init; }
    public string DomElementLabel   { get; init; }  // связанный <label> или aria-label
    public string DomFormAction     { get; init; }  // action формы → case_id кандидат
    public int    DomFormFieldCount { get; init; }  // количество полей в форме
    public string XhrMethod         { get; init; }  // GET/POST/PUT/PATCH/DELETE
    public int    XhrStatus         { get; init; }  // HTTP статус ответа

    // Скриншот
    public string ScreenshotPath    { get; init; }  // относительный путь или null
    public ulong  ScreenshotDHash   { get; init; }  // 0 если нет скриншота
    public string CaptureReason     { get; init; }  // "window_activated" | "periodic_10s" | "ui_event"

    // Vision (заполняется сервером)
    public bool   VisionDone        { get; init; }
    public bool   VisionSkipped     { get; init; }  // dHash-дубль — пропущен
    public string VisionTaskLabel   { get; init; }
    public string VisionAppContext  { get; init; }
    public string VisionActionType  { get; init; }  // DATA_ENTRY/NAVIGATION/SEARCH/APPROVAL/REPORT
    public string VisionCaseId      { get; init; }
    public bool   VisionIsCommit    { get; init; }
    public string VisionCognitive   { get; init; }  // LOW/MEDIUM/HIGH
    public float  VisionConfidence  { get; init; }
    public string VisionAutoNotes   { get; init; }

    public string CaseId => VisionCaseId ?? CaseIdCandidate;
    public string Payload { get; init; }  // полный JSON — страховка от изменений схемы
}
```

### EventType enum

```csharp
public enum EventType
{
    // Слой window
    WindowActivated, AppSwitch,

    // UIAutomation (слой window, best-effort)
    Invoked, ValueChanged, SelectionChanged, TextCommitted,
    ClipboardCopy, ClipboardPaste, HotkeyUndo,
    ErrorDialogAppeared, IdleStart, IdleEnd,

    // Слой visual
    Screenshot,

    // Слой system
    ProcessStart, ProcessStop,
    FileCreate, FileWrite,

    // Слой agent (служебные)
    HeartbeatPulse,   // раз в минуту: POST /api/v1/heartbeat → events_buffered, drift_ms
    SyncCompleted,    // после HTTP-батча: sent_count, failed_count
    LayerError,       // слой упал: layer_name, exception_message
    CommandReceived,    // команда получена из cmd/pending.json
    CommandExecuted,    // команда выполнена: command, status, message
    UpdateAvailable,    // обнаружена новая версия (GET /api/v1/updates/latest)
    UpdateStarted,      // начало процесса обновления
    UpdateCompleted,    // обновление завершено (пишет новая версия при старте)
    PerformanceSnapshot,// метрики нагрузки каждые 5 минут

    // Слой Д — логи приложений
    EventLogEntry,          // из Windows Event Log канала
    FileLogEntry,           // строка из файлового лога
    RecentDocumentOpened,   // Registry MRU изменился → документ открыт
    LnkCreated,             // новый LNK файл → документ открыт

    // Слой Е — браузер (Chrome + Edge)
    BrowserPageLoad,        // URL + title при загрузке страницы
    BrowserNavigation,      // SPA route change (pushState/popstate)
    BrowserTabActivated,    // переключение вкладки
    BrowserFormFieldFocus,  // поле в фокусе: name, label, type
    BrowserFormFieldBlur,   // поле потеряло фокус: value_length, value_type
    BrowserElementClick,    // клик: tag, id, text, href
    BrowserXhrRequest,      // URL + метод + статус (не тело)
    BrowserFormSubmit,      // отправка формы: action URL, field count

    // Добавляются сервером
    VisionContextAdded, TaskBoundaryDetected
}
```

### SQLite схема (клиент)

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;

CREATE TABLE IF NOT EXISTS events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id         TEXT NOT NULL UNIQUE,
    session_id       TEXT NOT NULL,
    machine_id       TEXT NOT NULL,
    user_id          TEXT NOT NULL,
    timestamp_utc    INTEGER NOT NULL,
    synced_ts        INTEGER NOT NULL,
    drift_ms         INTEGER NOT NULL DEFAULT 0,
    drift_rate_ppm   REAL    NOT NULL DEFAULT 0,
    sequence_idx     INTEGER NOT NULL,
    layer            TEXT NOT NULL,
    event_type       TEXT NOT NULL,
    process_name     TEXT,
    app_version      TEXT,
    window_title     TEXT,
    window_class     TEXT,
    element_type     TEXT,
    element_name     TEXT,
    element_auto_id  TEXT,
    case_id          TEXT,
    screenshot_path  TEXT,
    screenshot_dhash INTEGER,           -- ulong как INTEGER
    capture_reason   TEXT,
    -- Слой Д
    log_source       TEXT,
    log_level        TEXT,
    raw_message      TEXT,
    message_hash     TEXT,
    document_path    TEXT,
    document_name    TEXT,
    sent             INTEGER NOT NULL DEFAULT 0,  -- 0=pending 1=sent 2=failed
    sent_at          INTEGER,
    payload          TEXT NOT NULL
);

CREATE INDEX idx_unsent  ON events (sent, timestamp_utc);
CREATE INDEX idx_session ON events (session_id, sequence_idx);
```

### PostgreSQL схема (сервер)

```sql
CREATE TABLE events (
    id               BIGSERIAL PRIMARY KEY,
    event_id         UUID NOT NULL UNIQUE,
    session_id       UUID NOT NULL,
    machine_id       TEXT NOT NULL,
    user_id          TEXT NOT NULL,
    timestamp_utc    BIGINT NOT NULL,
    synced_ts        BIGINT NOT NULL,
    drift_ms         INTEGER NOT NULL DEFAULT 0,
    drift_rate_ppm   FLOAT   NOT NULL DEFAULT 0,
    sequence_idx     INTEGER NOT NULL,
    layer            TEXT NOT NULL,
    event_type       TEXT NOT NULL,
    process_name     TEXT,
    app_version      TEXT,
    window_title     TEXT,
    window_class     TEXT,
    element_type     TEXT,
    element_name     TEXT,
    element_auto_id  TEXT,
    case_id          TEXT,
    screenshot_path  TEXT,
    screenshot_dhash BIGINT,
    capture_reason   TEXT,
    -- Слой Д
    log_source       TEXT,
    log_level        TEXT,
    raw_message      TEXT,
    message_hash     TEXT,
    document_path    TEXT,
    document_name    TEXT,
    -- Vision
    vision_done      BOOLEAN DEFAULT FALSE,
    vision_skipped   BOOLEAN DEFAULT FALSE,
    vision_task_label   TEXT,
    vision_app_context  TEXT,
    vision_action_type  TEXT,
    vision_case_id      TEXT,
    vision_is_commit    BOOLEAN,
    vision_cognitive    TEXT,
    vision_confidence   FLOAT,
    vision_auto_notes   TEXT,
    -- Вычисляемый
    -- Третий источник case_id: имя документа из MRU/LNK
    resolved_case_id TEXT GENERATED ALWAYS AS (
        COALESCE(vision_case_id, case_id, document_name)
    ) STORED,
    payload          JSONB NOT NULL,
    loaded_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_session     ON events (session_id, synced_ts);
CREATE INDEX idx_machine_ts  ON events (machine_id, synced_ts);
CREATE INDEX idx_case        ON events (resolved_case_id) WHERE resolved_case_id IS NOT NULL;
CREATE INDEX idx_vision_todo ON events (vision_done, layer)
    WHERE layer = 'visual' AND vision_done = FALSE AND vision_skipped = FALSE;
```

**Серверная коррекция времени** — View строит непрерывную drift-кривую по heartbeat-точкам и применяет интерполяцию к каждому событию:

```sql
-- Опорные точки: drift_ms из каждого HeartbeatPulse
CREATE VIEW heartbeat_drift AS
SELECT
    session_id,
    synced_ts                                                      AS hb_ts,
    drift_ms,
    drift_rate_ppm,
    LEAD(synced_ts) OVER (PARTITION BY session_id ORDER BY synced_ts) AS next_hb_ts
FROM events
WHERE event_type = 'HeartbeatPulse';

-- Для каждого события: найти ближайший предшествующий heartbeat и интерполировать
CREATE VIEW events_corrected AS
SELECT
    e.*,
    e.synced_ts + COALESCE(
        (h.drift_ms + (h.drift_rate_ppm * (e.synced_ts - h.hb_ts) / 1000000.0))::BIGINT,
        e.drift_ms
    ) AS server_ts   -- итоговая временная метка для анализа
FROM events e
LEFT JOIN LATERAL (
    SELECT drift_ms, drift_rate_ppm, hb_ts
    FROM heartbeat_drift h
    WHERE h.session_id = e.session_id
      AND h.hb_ts <= e.synced_ts
    ORDER BY h.hb_ts DESC
    LIMIT 1
) h ON true;
```

`server_ts` — единая ось времени для всех машин. Используется вместо `synced_ts` во всех аналитических запросах.

**Корреляция событий между пользователями**: по `server_ts`. Ожидаемая точность: ±100мс при ntp_round_trip < 50мс, ±500мс при зашумлённой сети. Существенно лучше исходных ±2000мс без интерполяции.

---

## Server Processing Pipeline

Весь Python pipeline работает **на сервере** с уже захваченными данными. C# агент только собирает. MSS/PyAutoGUI/FFmpeg не используются — захват лучше решён в C#.

### Порядок стадий (ночной батч, cron 02:00)

```
PostgreSQL (raw events)
  │
  ├── Stage 1: Load        ← выбрать events WHERE layer='visual' AND vision_done=FALSE
  │                           + связанные window/system/applogs события в ±30 сек
  │
  ├── Stage 2: OCR         ← PaddleOCR по каждому скриншоту
  │                           выход: blocks[{text, bbox, confidence, language}]
  │                           стоимость: $0, офлайн
  │
  ├── Stage 3: UI Detection ← YOLOv8 по скриншоту
  │                           выход: ui_elements[{type, bbox, confidence, state}]
  │                           + layout regions (form/table/modal/sidebar)
  │
  ├── Stage 4: Screen Diff  ← сравнение с предыдущим скриншотом сессии
  │                           выход: screen_change_score, added_text, removed_text,
  │                                  appeared_elements, disappeared_elements
  │
  ├── Stage 5: Vision LLM   ← ТОЛЬКО если OCR+UI confidence < 0.75
  │   └── Claude API батч 20 (MVP; Qwen2.5-VL 7B через Ollama — после пилота)
  │                           выход: screen_type, action_hypothesis, entities,
  │                                  case_id_candidate, cognitive_demand
  │
  ├── Stage 6: Event Extraction ← rules + LLM hybrid, confidence scoring
  │                           вход: OCR + UI + Diff + Vision + C# agent events
  │                           выход: event_type (из словаря), target, confidence
  │
  ├── Stage 7: Second Pass   ← дедупликация, merge weak events,
  │                           case_id repair (каскад), recalc confidence,
  │                           transition validation
  │
  └── Stage 8: Process Mining ← pm4py: discovery, conformance, variants
                                выход: process graph + FTE metrics
```

### Two-tier Vision strategy (снижение стоимости)

```
Скриншот
  │
  ├── OCR + YOLO confidence >= 0.75 ?
  │     YES → Event Extraction без LLM (экономим ~70% вызовов)
  │     NO  → Claude API (батч 20; в production — Qwen локально)
  │
  └── Ожидаемое распределение:
        ~70% скриншотов: стандартные формы → OCR+YOLO достаточно
        ~30% скриншотов: нестандартные экраны → Vision LLM
```

### Enriched Event Schema (PostgreSQL `events_processed`)

После прохождения pipeline каждый visual-event получает обогащённую запись:

```json
{
  "event_id": "uuid",
  "session_id": "uuid",
  "user_id": "sha256",
  "machine_id": "sha256",
  "server_ts": 1700000000000,

  "context": {
    "app_name": "1cv8.exe",
    "app_version": "8.3.24",
    "window_title": "1С:Предприятие — Накладная №12345",
    "screen_resolution": [1920, 1080],
    "active_monitor": 1,
    "os": "windows"
  },

  "input": {
    "hotkeys": ["ctrl+s"],
    "input_type": "TEXT",
    "input_length": 12,
    "mouse_click": true,
    "mouse_button": "left",
    "mouse_position_relative": [0.42, 0.31],
    "mouse_position_abs": [806, 335],
    "double_click": false,
    "scroll": null,
    "scroll_delta": 0,
    "focused_element_id": "e1"
  },

  "screen": {
    "screenshot_id": "sha256_of_file",
    "screen_hash": "dhash_uint64",
    "previous_screen_hash": "dhash_uint64",
    "screen_change_score": 0.82,
    "is_duplicate": false,
    "capture_reason": "window_activated"
  },

  "screen_diff": {
    "added_text": ["Сохранено"],
    "removed_text": [],
    "appeared_elements": [{"type": "toast", "text": "Документ проведён"}],
    "disappeared_elements": [{"type": "modal", "id": "e5"}],
    "changed_fields": [{"element_id": "e1", "old_length": 0, "new_length": 12}]
  },

  "ocr": {
    "blocks": [
      {
        "text": "Сохранить",
        "bbox": [120, 450, 210, 475],
        "confidence": 0.98,
        "language": "ru",
        "normalized_text": "сохранить"
      }
    ],
    "full_text": "...",
    "dominant_language": "ru",
    "avg_confidence": 0.95
  },

  "ui_elements": [
    {
      "element_id": "e1",
      "type": "button",
      "bbox": [115, 445, 215, 480],
      "confidence": 0.94,
      "state": "active",
      "text": "Сохранить",
      "clickable": true,
      "visible": true
    }
  ],

  "layout": {
    "regions": [
      {"type": "form_region", "bbox": [0, 100, 1920, 900]},
      {"type": "header",      "bbox": [0, 0,   1920, 100]}
    ],
    "has_modal": false,
    "has_table": false
  },

  "semantic": {
    "screen_type": "document_form",
    "entities": [
      {"type": "document_id", "value": "12345"},
      {"type": "counterparty", "value": "ООО Альфа"}
    ],
    "case_id_candidate": "12345",
    "case_id_source": "ocr_entity"
  },

  "vision_llm": {
    "used": false,
    "model": null,
    "description": null,
    "action_hypothesis": null,
    "confidence": null,
    "cognitive_demand": "LOW"
  },

  "event": {
    "event_type": "submit_form",
    "target": "Сохранить",
    "target_type": "button",
    "target_bbox": [115, 445, 215, 480],
    "value": null,
    "confidence": 0.93,
    "confidence_breakdown": {
      "input_signal": 0.25,
      "ui_detection": 0.25,
      "ocr_match": 0.20,
      "visual_delta": 0.20,
      "llm_hypothesis": 0.00
    },
    "source": "rule",
    "status": "trusted"
  },

  "process": {
    "case_id": "12345",
    "case_id_source": "ocr_entity",
    "step_name": "submit_form",
    "step_index": 4,
    "is_loop": false,
    "is_deviation": false
  },

  "quality": {
    "ocr_confidence_avg": 0.95,
    "ui_detection_confidence_avg": 0.92,
    "event_confidence": 0.93,
    "status": "trusted",
    "requires_review": false,
    "review_reason": null
  },

  "debug": {
    "pipeline_version": "v1.0",
    "models_used": {
      "ocr": "PaddleOCR",
      "detector": "YOLOv8",
      "vlm": null
    },
    "processing_ts": 1700086400000
  }
}
```

### Event Type Dictionary (нормализованный словарь)

```python
# UI Events (атомарные действия)
UI_EVENTS = {
    "open_screen", "close_screen",
    "click_button", "double_click_button",
    "edit_field", "clear_field",
    "select_dropdown", "select_checkbox", "select_radio",
    "open_modal", "close_modal",
    "open_tab", "switch_tab", "close_tab",
    "scroll_list", "scroll_page",
    "copy_text", "paste_text",
    "view_table", "sort_table", "filter_table",
    "expand_section", "collapse_section",
    "upload_file", "download_file",
    "submit_form", "cancel_form",
    "hotkey_action",
    "weak_click",        # клик без визуального отклика
    "ambiguous_action",  # не удалось классифицировать
}

# Business Events (формируются из набора UI-событий, не из одного кадра)
BUSINESS_EVENTS = {
    "create_document", "edit_document", "save_document",
    "approve_document", "reject_document",
    "send_message", "receive_message",
    "create_task", "complete_task",
    "export_report", "print_document",
    "search_record", "open_record",
}
# Правило: business event = подтверждённый набор UI-событий + visual commit signal
```

### Event Extraction Rules

**Правило 1 — Минимум 2 сигнала для события**
Любое значимое событие подтверждается минимум 2 источниками:
- input signal (click / key / scroll)
- visual change (screen_change_score > 0.1 OR appeared/disappeared element)

Одиночный сигнал → `weak_click` или `ambiguous_action`, не бизнес-событие.

**Правило 2 — LLM только гипотеза, не источник истины**
Приоритет источников:
1. explicit input + visual confirmation (rule → `source: "rule"`)
2. OCR text + UI element fusion (rule → `source: "rule"`)
3. LLM hypothesis (→ `source: "llm"`, снижает итоговый confidence)

**Правило 3 — OCR и UI привязка только пространственно**
- bbox текста внутри bbox элемента → text/value этого элемента
- bbox текста слева от input → label
- bbox текста сверху над input → label
- bbox текста внутри table region → cell/header, не форма

**Правило 4 — edit_field только при изменении value**
Событие `edit_field` только если: keyboard_input + field focused + OCR value изменился на следующем кадре.

**Правило 5 — click_button только при попадании в bbox**
`click_button` только если: mouse_click=true + координата внутри bbox clickable элемента + элемент visible и не disabled.
Клик без визуального изменения → `weak_click`.

**Правило 6 — анализировать тройку кадров**
Всегда: previous → current → next. Вычислять screen_diff по тройке.
Без sequence теряется большая часть логики (особенно submit → confirmation).

**Правило 7 — дедупликация обязательна**
Если screen_change_score < 0.05 и нет input событий → не создавать process event.
Серия одинаковых событий → схлопнуть в одно с `count` и `duration_ms`.

**Правило 8 — мелкие элементы (checkbox, radio, icon) отдельным классом**
При необходимости crop+zoom зоны. Не доверять общему детектору без порогового подтверждения.

**Правило 9 — таблицы как отдельный контейнер**
Сначала `table_region`, потом: header row → cell values → selected row → sort indicators.
Текст таблицы не смешивать с текстом формы.

**Правило 10 — case_id каскадом**
1. Явный ID с экрана (OCR entity: order_id / doc_id / client_id)
2. Из window_title (regex)
3. Из Registry MRU / LNK (document_name)
4. Временный `session_case_{hash}` → post-hoc merge по overlapping entities

**Правило 11 — UI events vs Business events строго разделены**
`submit_form` (UI) ≠ `save_document` (Business).
Business event формируется только после подтверждения набора UI-событий + visual commit signal (toast, redirect, confirmation dialog).

**Правило 12 — confidence как агрегат**
```python
event_confidence = (
    0.25 * has_input_signal +
    0.25 * (ui_detection_confidence >= 0.90) +
    0.20 * ocr_text_matches_target +
    0.20 * visual_change_confirmed +
    0.10 * llm_agrees_with_rules
)
# >= 0.90 → trusted | 0.75–0.89 → probable | < 0.75 → requires_review
```

**Правило 13 — слабые события не удалять, маркировать**
`requires_review=true` с `review_reason`: `low_ocr_confidence` / `ambiguous_target` / `no_visual_change` / `no_input_signal`.
Это обучающие данные для улучшения моделей.

**Правило 14 — reference screen templates**
Для частых экранов хранить anchors: набор bbox+text пар которые однозначно идентифицируют экран.
`screen_type` определяется сначала по шаблону, потом Vision LLM.

**Правило 15 — интеграция данных C# агента**
UIAutomation события → подтверждение input signal (element_id + event_type)
Registry MRU / LNK → case_id_candidate (document_name)
Windows Event Log → review_reason при ошибках приложения

**Правило 16 — second-pass validation обязателен**
После первичного extraction: дедупликация → merge weak events → repair missing transitions → normalize case_id → recalc confidence.
Второй проход поднимает качество c MVP до production.

**Правило 17 — error buckets для улучшения системы**
Все ошибки категоризировать: `ocr_miss` / `ui_miss` / `wrong_binding` / `hallucinated_event` / `duplicate_event` / `wrong_case_id` / `transition_error`.
База для улучшения YOLO, промптов, правил.

---

## Структура репозитория

```
/
├── src/
│   └── WinDiagSvc/                     ← публичное имя: Windows Diagnostics Service
│       ├── Program.cs
│       ├── appsettings.json
│       ├── Capture/
│       │   ├── WindowWatcher.cs        ← WinEventHook + GetWindowText + ProcessName
│       │   ├── ScreenshotWorker.cs     ← GDI+ capture + SkiaSharp WebP + dHash
│       │   ├── UiAutomationCapture.cs  ← best-effort, все события без фильтра
│       │   ├── ClipboardMonitor.cs
│       │   ├── IdleDetector.cs         ← GetLastInputInfo
│       │   ├── ProcessWatcher.cs       ← WMI ManagementEventWatcher
│       │   ├── FileEventCapture.cs     ← ETW Kernel-File, фильтр по расширениям
│       │   ├── NtpSynchronizer.cs      ← fallback chain, 2 мин, медиана 5 запросов, drift_rate_ppm
│       │   └── AppLogScanner/
│       │       ├── AppLogScanner.cs        ← координатор, discovery при старте
│       │       ├── EventLogWatcher.cs      ← все каналы Windows Event Log, real-time
│       │       ├── FileLogScanner.cs       ← discovery + FileSystemWatcher + delta read
│       │       ├── RegistryMruReader.cs    ← polling 30s → RecentDocumentOpened
│       │       └── LnkWatcher.cs           ← FileSystemWatcher на Recent\, LNK parsing
│       ├── Browser/
│       │   └── BrowserMessageHost.cs   ← Native Messaging stdin/stdout → EventStore
│       ├── Management/
│       │   ├── CommandPoller.cs        ← polling GET /api/v1/commands/{machine_id} раз в 60с
│       │   ├── UpdateManager.cs        ← проверка GET /api/v1/updates/latest, запуск апдейтера
│       │   └── PerformanceMonitor.cs   ← сбор метрик CPU/RAM/очередь каждые 5 мин
│       ├── Storage/
│       │   └── EventStore.cs           ← единственный SqliteConnection
│       ├── Sync/
│       │   └── EventSyncWorker.cs      ← HTTP батч POST /api/v1/events, retry sent=0/1/2
│       └── Models/
│           ├── ActivityEvent.cs
│           └── EventType.cs
│
├── browser-extension/                  ← Chrome + Edge (один codebase, Manifest V3)
│   ├── manifest.json                   ← permissions: tabs, webNavigation, webRequest, nativeMessaging
│   ├── background.js                   ← Service Worker: tabs, navigation, XHR interception
│   ├── content.js                      ← Content Script: DOM, form fields, focus/blur, clicks
│   ├── native-messaging-host.json      ← регистрация Native Messaging host
│   └── icons/
│       └── icon16.png                  ← нейтральная иконка (не привлекает внимание)
│
├── installer/
│   ├── install.ps1                     ← единственная точка входа: всё ставится одной командой
│   ├── nssm.exe                        ← bundled
│   ├── WinDiagSvc.exe                  ← bundled (self-contained .NET 8, не требует runtime)
│   ├── appsettings.json                ← шаблон, MachineId/UserId заполняет агент при старте
│   ├── extension.crx                   ← упакованное расширение Chrome/Edge, bundled
│   ├── native-messaging-host.json      ← манифест Native Messaging, bundled
│   └── WinDiagUpdater.ps1              ← автономный апдейтер (запускается вне сервиса)
│
├── server/
│   ├── docker-compose.yml
│   ├── .env.example
│   ├── manage.py                       ← CLI: status / restart / logs / update / perf
│   ├── etl/
│   │   └── load_events.py              ← (legacy) нет SMB; события приходят напрямую через HTTP
│   ├── pipeline/
│   │   ├── run_pipeline.py             ← координатор всех стадий (cron 02:00)
│   │   ├── stage1_load.py              ← выбрать необработанные visual events + контекст
│   │   ├── stage2_ocr.py               ← PaddleOCR по скриншотам
│   │   ├── stage3_ui_detection.py      ← YOLOv8 детекция элементов + layout
│   │   ├── stage4_screen_diff.py       ← screen_change_score, added/removed text, element delta
│   │   ├── stage5_vision_llm.py        ← Claude API батч 20 (MVP); интерфейс готов под Ollama
│   │   ├── stage6_event_extraction.py  ← rules + LLM hybrid, confidence scoring
│   │   ├── stage7_second_pass.py       ← dedup, merge, case_id repair, recalc
│   │   ├── confidence_scorer.py        ← event_confidence = weighted sum
│   │   ├── event_type_dict.py          ← нормализованный словарь UI + Business событий
│   │   └── models/
│   │       └── yolo_ui/                ← веса YOLOv8 для UI элементов
│   ├── analytics/
│   │   ├── reconstruct_tasks.py        ← task sequences → task instances
│   │   ├── process_mining.py           ← pm4py: discovery + conformance
│   │   ├── role_clustering.py          ← HDBSCAN по task-mix векторам
│   │   └── fte_analysis.py
│   └── notebooks/
│       └── analysis.ipynb
│
└── CLAUDE.md
```

---

## Правила работы агента (karpathy-guidelines)

Эти правила активны при любом кодинге и редактировании.

1. **Think Before Coding** — называй допущения вслух перед стартом. При любой неоднозначности задачи — сначала спроси, потом пиши код. Не молчи о предположениях.

2. **Simplicity First** — пиши только то, что нужно прямо сейчас. После каждого решения: self-check "не overcomplicated?". Никаких абстракций "на будущее".

3. **Surgical Edits** — трогай только то, что явно попросили. Соседний код, комментарии, структуру — не трогать без явной необходимости. Мёртвый код: упомяни, не удаляй самостоятельно.

4. **Goal-Driven Execution** — для отладки баг: сначала тест воспроизводящий баг, потом исправление. Цикл до чёткого критерия успеха — не останавливаться на "наверное работает".

---

## Ключевые правила разработки

### Скрытый режим работы

Агент работает полностью невидимо для пользователя. Используются только легитимные механизмы Windows — без руткит-техник, без скрытия из Task Manager (антивирус заблокирует).

**Нейтральные имена** — ничто не указывает на мониторинг:

| Что | Значение |
|---|---|
| Имя процесса (exe) | `WinDiagSvc.exe` |
| Имя сервиса (sc) | `WinDiagSvc` |
| Display name сервиса | `Windows Diagnostics Service` |
| Путь установки | `C:\Program Files\Windows Diagnostics\` |
| Данные и скриншоты | `%ProgramData%\Microsoft\Diagnostics\` |
| Источник в Event Log | `WinDiagSvc` |
| Конфиг файл | `appsettings.json` → секция `DiagSettings` (не `AgentSettings`) |

**Сборка без консольного окна:**
```xml
<!-- TaskMining.Agent.csproj -->
<OutputType>WinExe</OutputType>   <!-- не Exe, не создаёт консоль -->
<ApplicationIcon></ApplicationIcon>
```

**Program.cs — только Windows Service, без консольного хоста:**
```csharp
Host.CreateDefaultBuilder(args)
    .UseWindowsService(o => o.ServiceName = "WinDiagSvc")
    .ConfigureLogging(l => l.ClearProviders())  // никакого stdout в production
    .ConfigureServices(...)
    .Build().Run();
```

**Что пользователь видит:**
- В Task Manager → Services: `WinDiagSvc` (Windows Diagnostics Service) — как сотни других системных сервисов
- В Process list: `WinDiagSvc.exe` — без окон, без иконки в трее
- В Program Files: папка `Windows Diagnostics` — не выделяется среди системных
- Нет уведомлений, нет balloon tips, нет звуков

**Что пользователь НЕ видит:**
- Иконка в системном трее — отсутствует (Windows Service не имеет UI)
- Окно консоли — `OutputType=WinExe` убирает
- Заметная нагрузка — `BelowNormal` приоритет

**Defender exclusion** через install.ps1 добавляет исключение по нейтральному пути:
```powershell
Add-MpPreference -ExclusionPath "C:\Program Files\Windows Diagnostics"
Add-MpPreference -ExclusionPath "$env:ProgramData\Microsoft\Diagnostics"
```

### C# агент

- **Один `SqliteConnection`** на весь lifetime сервиса. `Pooling=False`. Три слоя пишут через него — WAL справляется.
- Запись синхронная. Никаких Channel, батчей, таймеров на запись.
- Слой = отдельный `BackgroundService`. Каждый в `try/catch` на верхнем уровне. Исключение → `LayerError` событие → слой рестартует через 30 секунд.
- `ProcessPriorityClass.BelowNormal`, I/O потоки `ThreadPriority.Lowest`.
- Сервис запускается как **LocalSystem** (нужно для ETW).
- Пароли (`IsPasswordField = true`) → скриншот не делаем, событие пишем без хэша и без `ScreenshotPath`.
- `AppVersion` берём из `FileVersionInfo.GetVersionInfo(proc.MainModule.FileName).ProductVersion`.
- `MachineId` / `UserId` генерируются при первом старте и записываются в `appsettings.json`.
- dHash считается на каждом скриншоте. Если `HashDistance(prevDHash, newDHash) < 10` и `captureReason == "periodic_10s"` → скриншот не сохраняем, `ScreenshotPath = null`, `VisionSkipped = true` уже на клиенте.
- `HeartbeatPulse` раз в минуту: `POST /api/v1/heartbeat` с `events_buffered`, `drift_ms`.

### EventSyncWorker

- Накапливает события в SQLite (`sent=0`), батчами шлёт `POST /api/v1/events` на сервер.
- `sent`: 0 = pending, 1 = sent, 2 = failed (retry при следующем цикле).
- При ошибке сети: `sent=2`, пишет `LayerError`, ждёт следующего цикла.
- После успешной отправки: `UPDATE events SET sent=1, sent_at=?`.
- `SyncCompleted` событие после каждого батча.

### Python сервер

- Vision запускается ночью (cron 02:00) или вручную. Не в реалтайм.
- **Батч 20 скриншотов** на один вызов Claude. Системный промпт один на весь батч.
- Перед Vision: `dhash_filter.py` помечает `vision_skipped=true` для дублей (hamming < 10 в рамках сессии).
- События попадают в PostgreSQL напрямую через HTTP (event_queue → writer). ETL из SQLite не используется.
- Все конфиги из `.env` файла.

### Vision промпт — структура ответа

```json
[{
  "screenshot_index": 1,
  "task_label": "Ввод входящей накладной",
  "app_context": "Форма документа 1С — накладная",
  "action_type": "DATA_ENTRY",
  "visible_fields": ["Контрагент", "Сумма", "Дата"],
  "case_id_candidate": "12345",
  "completion_signal": false,
  "cognitive_demand": "LOW",
  "automation_notes": "Стандартная форма, все поля подписаны — подходит для RPA",
  "confidence": 0.92
}]
```

### Реконструкция задач (`reconstruct_tasks.py`)

```python
# Задача = непрерывная последовательность скриншотов с одним task_label
# Граница задачи = смена task_label ИЛИ пауза > 5 минут ИЛИ смена user_id
# На выходе: DataFrame с колонками:
#   user_id, task_label, start_ts, end_ts, duration_min,
#   screenshot_count, event_count, process_names, case_id

# Обогащение case_id из слоя Д:
# Для каждой задачи без resolved_case_id ищем RecentDocumentOpened / LnkCreated
# в окне server_ts ± 60 секунд → берём document_name как case_id_candidate
# Приоритет: vision_case_id > regex_case_id > document_name (MRU/LNK)
```

### Корреляция логов приложений с задачами (сервер)

```sql
-- Обогащаем case_id из document_name слоя Д
-- Запускается после Vision-обработки
UPDATE events e
SET    case_id = al.document_name
FROM   events al
WHERE  al.layer        = 'applogs'
  AND  al.document_name IS NOT NULL
  AND  al.session_id   = e.session_id
  AND  ABS(al.server_ts - e.server_ts) < 60000
  AND  e.vision_case_id IS NULL
  AND  e.case_id        IS NULL;

-- Сводка: какие лог-сообщения коррелируют с каждым task_label
-- Используется для ручного анализа и выявления ошибок приложений
CREATE VIEW task_log_correlation AS
SELECT
    e.user_id,
    e.resolved_case_id,
    v.vision_task_label,
    al.log_source,
    al.log_level,
    al.raw_message,
    al.server_ts - e.server_ts AS offset_ms
FROM events e
JOIN events v  ON v.session_id = e.session_id
             AND ABS(v.server_ts - e.server_ts) < 5000
             AND v.layer = 'visual'
             AND v.vision_task_label IS NOT NULL
JOIN events al ON al.session_id = e.session_id
             AND ABS(al.server_ts - e.server_ts) < 30000
             AND al.layer = 'applogs'
WHERE e.layer = 'window'
  AND e.event_type = 'WindowActivated';
```

---

## Конфигурация (appsettings.json)

```json
{
  "AgentSettings": {
    "MachineId": "",
    "UserId": "",
    "DbPath": "%ProgramData%\\Microsoft\\Diagnostics\\events.db",
    "ScreenshotDir": "%ProgramData%\\Microsoft\\Diagnostics\\cache",
    "ServerUrl": "https://srv-xxxx.seamlean.com",
    "SyncIntervalSeconds": 120,
    "ScreenshotIntervalSeconds": 10,
    "DHashDistanceThreshold": 10,
    "IdleLightThresholdMs": 30000,
    "IdleDeepThresholdMs": 120000,
    "NtpServers": ["pool.ntp.org", "time.windows.com", "time.nist.gov"],
    "NtpIntervalMinutes": 10,
    "HeartbeatIntervalSeconds": 60,
    "FileExtensionsToTrack": [".doc",".docx",".xls",".xlsx",".pdf",".csv",".txt",".xml",".1cd",".mxl"],
    "CaseIdPatterns": [
      { "ProcessName": "1cv8.exe",   "Pattern": "№\\s*(\\d+)" },
      { "ProcessName": "chrome.exe", "Pattern": "(?:Order|Заказ)[\\s#-]*(\\d+)" }
    ]
  }
}
```

---

## FTE-анализ

```python
def automation_score(process_df):
    executions     = len(process_df)
    variants       = process_df['event_sequence_hash'].nunique()
    repeatability  = 1 - (variants / executions)
    exception_rate = (process_df['has_undo'] | process_df['has_error']).mean()
    avg_duration   = process_df['duration_min'].mean()
    freq_per_day   = executions / process_df['date'].nunique()
    raw = (freq_per_day * avg_duration * repeatability) / max(exception_rate, 0.01)
    return raw  # нормировать min-max по всем процессам после получения реальных данных

def automation_type(score_normalized):
    if score_normalized >= 0.7: return 'RPA'
    if score_normalized >= 0.3: return 'Hybrid'
    return 'AI-Agent'

def fte_saving(avg_duration_min, executions_per_day):
    return round(avg_duration_min * executions_per_day / 480, 3)
```

---

## Управление клиентами (сервер → клиент)

### Архитектура команд

Канал связи — HTTP API сервера (та же инфраструктура, что и события).

```
Сервер пишет в БД:   INSERT INTO commands (machine_id, command, ...)
Клиент запрашивает:  GET /api/v1/commands/{machine_id}  (polling 60с)
Клиент шлёт ответ:   POST /api/v1/commands/{command_id}/ack
```

### Формат команды (pending.json)

```json
{
  "command_id": "uuid",
  "command":    "restart | status_dump | stop | start | update_config",
  "issued_at":  "2024-01-15T03:00:00Z",
  "issued_by":  "manual",
  "params":     {}
}
```

### Формат ответа (ack.json)

```json
{
  "command_id":   "uuid",
  "machine_id":   "sha256",
  "executed_at":  "2024-01-15T03:01:05Z",
  "status":       "ok | error",
  "message":      "Service restarted successfully",
  "service_state": "running | stopped",
  "events_buffered": 142,
  "drift_ms":     -312
}
```

### Клиент — CommandPoller.cs

```csharp
// BackgroundService, polling раз в 60 секунд
// GET {ServerUrl}/api/v1/commands/{MachineId}

private async Task ExecuteCommandAsync(AgentCommand cmd) => cmd.Command switch
{
    "restart"      => RestartService(),      // sc stop → sc start через SCM
    "stop"         => StopService(),
    "start"        => StartService(),
    "status_dump"  => DumpStatus(),          // пишет подробный статус в ack.json
    "update_config"=> UpdateConfig(cmd.Params), // перезаписать appsettings.json + restart
    _              => WriteAck(cmd, "error", $"Unknown command: {cmd.Command}")
};

private void RestartService()
{
    WriteAck(cmd, "ok", "Restarting...");
    // Рестарт через SCM — сервис перезапускает сам себя
    using var sc = new ServiceController("WinDiagSvc");
    sc.Stop(); sc.WaitForStatus(ServiceControllerStatus.Stopped, TimeSpan.FromSeconds(30));
    sc.Start();
}
```

Если команда старше 10 минут — игнорируем (устаревшая), шлём ack `expired`.

### Сервер — manage.py

```bash
# Статус всех машин (из PostgreSQL по последнему HeartbeatPulse)
python manage.py status

# Вывод:
# MACHINE_ID    LAST_SEEN        LAG      BUFFERED  DRIFT_MS  STATUS
# a3f1...       2024-01-15 10:42  1m 12s   0         -42       🟢 online
# b7c2...       2024-01-15 09:15  1h 27m   3841      —         🔴 offline
# d9e4...       2024-01-15 10:40  3m 05s   12        +812      🟡 warning (drift)

# Перезапустить одну машину
python manage.py restart a3f1...

# Перезапустить все офлайн-машины
python manage.py restart --offline

# Перезапустить все машины
python manage.py restart --all

# Показать последние LayerError по машине
python manage.py logs a3f1... --errors --tail 20

# Показать статус ack (выполнена ли команда)
python manage.py ack a3f1...

# Обновить конфиг на машине (например, изменить интервал скриншотов)
python manage.py update-config a3f1... --set ScreenshotIntervalSeconds=30
```

### Статусы машин

| Статус | Условие |
|---|---|
| 🟢 online | HeartbeatPulse < 2 минуты назад |
| 🟡 warning | HeartbeatPulse 2–15 минут назад ИЛИ drift_ms > 1000 ИЛИ events_buffered > 5000 |
| 🔴 offline | HeartbeatPulse > 15 минут назад |
| ❓ never | HeartbeatPulse не получен ни разу |

---

## Обновление клиентов (OTA)

### Механизм

Сервис не может заменить собственный exe пока работает. Решение: `UpdateManager` запускает автономный `WinDiagUpdater.ps1` как отдельный процесс через Scheduled Task, затем завершается. Скрипт живёт независимо от сервиса.

Пакеты обновлений — GitHub Releases (git-first). Агент скачивает напрямую по HTTPS.

```
Клиент (UpdateManager.cs):
  1. При старте + раз в час: GET {ServerUrl}/api/v1/updates/latest
  2. Сравнивает с текущей версией (из AssemblyVersion)
  3. Если версия новее → скачивает zip по download_url, проверяет SHA256
  4. Распаковывает в staging папку
  5. Создаёт Windows Scheduled Task "WinDiagUpdate" → запускает WinDiagUpdater.ps1
  6. Пишет CommandExecuted событие и завершает сервис

WinDiagUpdater.ps1 (автономный, запущен как LocalSystem):
  7. Ждёт 5 сек (сервис останавливается)
  8. net stop WinDiagSvc
  9. Copy-Item staging\* → "C:\Program Files\Windows Diagnostics\"
  10. net start WinDiagSvc
  11. Удаляет Scheduled Task и staging папку
```

### latest.json (GET /api/v1/updates/latest)

```json
{
  "version":      "1.1.0",
  "released_at":  "2024-02-01T00:00:00Z",
  "download_url": "https://github.com/.../releases/download/agent%2Fv1.1.0/WinDiagSvc.zip",
  "sha256":       "abc123...",
  "min_version":  "1.0.0",
  "changelog":    "Added PerformanceMonitor, fixed ETW on Win10 21H2"
}
```

### Команды manage.py

```bash
# Опубликовать новую версию (через GitHub Release → webhook обновляет latest.json на сервере)
python manage.py update --version 1.1.0

# Принудительно обновить конкретную машину сейчас
python manage.py update --force a3f1...

# Откатить на предыдущую версию
python manage.py update --rollback a3f1...

# Статус обновлений по всем машинам
python manage.py update --status
# Вывод:
# MACHINE_ID   CURRENT   LATEST   STATUS
# a3f1...      1.0.0     1.1.0    🔄 pending update
# b7c2...      1.1.0     1.1.0    ✅ up to date
# d9e4...      1.1.0     1.1.0    ✅ up to date
```

---

## Метрики нагрузки агента

### PerformanceMonitor.cs

Новый `BackgroundService`. Каждые **5 минут** собирает метрики и пишет `PerformanceSnapshot` в SQLite → уходит на сервер при следующем HTTP-батче EventSyncWorker.

```json
{
  "event_type": "PerformanceSnapshot",
  "layer": "agent",
  "payload": {
    "agent_version":         "1.1.0",
    "process_cpu_pct":       0.8,
    "process_ram_mb":        94,
    "sqlite_size_mb":        12.4,
    "screenshots_size_mb":   187.3,
    "events_pending":        0,
    "events_failed":         3,
    "events_rate_per_min":   67,
    "screenshot_rate_per_min": 6,
    "sync_lag_sec": 42,
    "ntp_drift_ms":          -312,
    "ntp_drift_rate_ppm":    4.7,
    "ntp_last_sync_ago_min": 1,
    "layer_stats": {
      "window":   { "events_5min": 23, "errors_5min": 0 },
      "visual":   { "events_5min": 30, "errors_5min": 0 },
      "system":   { "events_5min":  8, "errors_5min": 0 },
      "applogs":  { "events_5min": 12, "errors_5min": 1 },
      "browser":  { "events_5min": 45, "errors_5min": 0 },
      "agent":    { "events_5min":  6, "errors_5min": 0 }
    }
  }
}
```

### PostgreSQL view для анализа нагрузки

```sql
CREATE VIEW agent_performance AS
SELECT
    machine_id,
    server_ts,
    (payload->>'agent_version')                    AS version,
    (payload->>'process_cpu_pct')::FLOAT           AS cpu_pct,
    (payload->>'process_ram_mb')::FLOAT            AS ram_mb,
    (payload->>'sqlite_size_mb')::FLOAT            AS db_mb,
    (payload->>'screenshots_size_mb')::FLOAT       AS screenshots_mb,
    (payload->>'events_pending')::INT              AS pending,
    (payload->>'events_failed')::INT               AS failed,
    (payload->>'events_rate_per_min')::INT         AS rate_per_min,
    (payload->>'ntp_drift_ms')::INT                AS drift_ms,
    (payload->>'sync_lag_sec')::INT / 60           AS sync_lag_min,
    payload->'layer_stats'                         AS layer_stats
FROM events
WHERE event_type = 'PerformanceSnapshot'
ORDER BY server_ts DESC;
```

### manage.py perf

```bash
# Текущая нагрузка по всем машинам (последний snapshot)
python manage.py perf

# Вывод:
# MACHINE  VER    CPU%  RAM_MB  DB_MB  PENDING  SYNC_LAG  DRIFT_MS  RATE/MIN
# a3f1...  1.1.0  0.8   94      12.4   0        42m       -312      67
# b7c2...  1.1.0  1.2   102     18.7   3841     89m ⚠️    +44       71
# d9e4...  1.1.0  0.5   88      9.1    0        38m       -18       58

# История нагрузки машины за период
python manage.py perf a3f1... --from 2024-01-15 --to 2024-01-16

# Найти машины с проблемами
python manage.py perf --warnings
# pending > 1000 | sync_lag > 120m | cpu > 10% | drift_ms > 1000
```

---

## Безопасность

- LocalSystem — только для ETW. Минимально необходимые права.
- UIAutomation и WinEvent — accessibility API, не низкоуровневые хуки.
- Нет DLL injection. Нет доступа к памяти чужих процессов.
- HTTPS с клиента — только до CF Tunnel сервера (порт 443). Прямых входящих соединений нет.
- Claude API — только с сервера.
- Пароли: `IsPasswordField = true` → событие без хэша, без скриншота.

---

## Объём данных (10 машин, пилот)

| Метрика | Значение |
|---|---|
| Событий в день / пользователь | ~4 000 |
| Скриншотов в день / пользователь | ~800 raw → ~400 после dHash |
| Размер скриншота WebP | ~25 КБ |
| Данных в день / пользователь | ~14 МБ скриншоты + ~4 МБ SQLite = ~18 МБ |
| Данных за месяц / 10 машин | ~5.4 ГБ |
| Vision вызовов / месяц | ~6 000 (400 скриншотов × 10 машин × 30 дней / батч 20) |
| Vision стоимость / месяц | **~$720** (input ~$360 + output ~$360) |

---

## Требования к железу

### Клиентские машины

| | |
|---|---|
| OS | Windows 10/11 x64 |
| .NET | Runtime 8.0 — bundled, не требует установки отдельно |
| Chrome / Edge | Любая актуальная версия (расширение ставится автоматически) |
| RAM overhead | +80–120 МБ |
| CPU overhead | ~1–2% |
| Диск | ~130 МБ буфер (18 МБ/день × 7 дней) |
| Сеть | HTTPS доступ до CF Tunnel сервера (порт 443) |
| Права при установке | Локальный администратор |
| Действий от пользователя | **Ноль** — ИТ запускает одну команду, всё остальное автоматически |

### Сервер (пилот 10 машин)

| | Минимум | Рекомендуется |
|---|---|---|
| CPU | 4 ядра | 8 ядер |
| RAM | 8 ГБ | 16 ГБ |
| Диск | 200 ГБ SSD | 500 ГБ SSD |
| ОС | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS |
| Сеть | 100 Мбит | Гигабит |
| Docker | 24+ | 24+ |

Раскладка диска (3 месяца пилота):
```
Скриншоты WebP:      ~16 ГБ  (5.4 ГБ/мес × 3)
PostgreSQL events:   ~4 ГБ
PostgreSQL WAL/idx:  ~2 ГБ
SQLite архив (ETL):  ~3 ГБ
Итого:              ~25 ГБ  → 200 ГБ с запасом
```

---

## Порядок разработки

```
Неделя 1: Инфраструктура агента + сервер (параллельно)

  [Клиент — C# агент]
  □ ActivityEvent + EventType + SQLite schema (все поля включая layer Д)
  □ EventStore (один SqliteConnection, WAL, синхронная запись)
  □ NtpSynchronizer (fallback chain, 2 мин, drift_rate_ppm, медиана 5 запросов)
  □ EventSyncWorker (HTTP батч POST /api/v1/events, retry sent=0/1/2, SyncCompleted событие)
  □ HeartbeatPulse (раз в минуту)
  □ LayerError handling (каждый слой в try/catch → LayerError → restart через 30с)
  □ install.ps1 (NSSM + Defender exclusion + MachineId/UserId генерация)

  [Сервер — параллельно]
  □ docker-compose.yml (PostgreSQL 16 + cron)
  □ PostgreSQL schema (events + индексы)
  □ events_corrected view (server_ts через drift interpolation)
  □ FastAPI event_queue → PostgreSQL writer (события приходят через HTTP, ETL из файлов не нужен)
  □ Heartbeat monitor (нет событий > 15 мин → лог алерт)
  □ CF Tunnel настроен, ServerUrl доступен с клиентских машин

  РЕЗУЛЬТАТ НЕДЕЛИ 1:
  Сервер принимает данные. Агент собирает базовые события.
  Первый батч EventSyncWorker проверяет связку агент → CF Tunnel → PostgreSQL.

Неделя 2: Все слои захвата (полный агент)
  □ WindowWatcher     — WinEventHook + GetWindowText + ProcessName + AppVersion
  □ ScreenshotWorker  — GDI+ capture + WebP + dHash + CaptureReason
  □ UiAutomationCapture — все события без фильтра, пустые поля пишем как есть
  □ ClipboardMonitor  — Copy/Paste cross-app
  □ IdleDetector      — GetLastInputInfo, IdleStart/IdleEnd
  □ CaseId regex      — из WindowTitle по конфигурируемым паттернам
  □ ProcessWatcher    — WMI ManagementEventWatcher (ProcessStart/Stop)
  □ FileEventCapture  — ETW Kernel-File, фильтр по расширениям
  □ AppLogScanner:
      □ EventLogWatcher   — GetLogNames() discovery + подписка на активные каналы
      □ RegistryMruReader — polling 30s, RecentDocumentOpened → case_id
      □ LnkWatcher        — FileSystemWatcher на Recent\, LNK parsing
      □ FileLogScanner    — discovery %AppData%/%ProgramData% + delta read
  □ BrowserMessageHost.cs — Native Messaging stdin/stdout → EventStore
  □ CommandPoller.cs — polling GET /api/v1/commands/{machine_id} каждые 60с
  □ UpdateManager.cs — проверка latest.json при старте + раз в 10 минут, Scheduled Task апдейтер
  □ WinDiagUpdater.ps1 — автономный апдейтер (stop → copy → start)
  □ PerformanceMonitor.cs — PerformanceSnapshot каждые 5 мин
  □ browser-extension/manifest.json + background.js + content.js (Chrome + Edge)
  □ install.ps1 — NSSM + Defender + Native Messaging + ExtensionInstallForcelist
  □ manage.py — CLI: status / restart / logs / update / perf

  РЕЗУЛЬТАТ НЕДЕЛИ 2:
  Агент установлен на все 10 машин → все слои работают → видим полный поток событий,
  проверяем LayerError логи по каждому слою на каждой машине,
  проверяем корреляцию server_ts между машинами (drift_ms расходится?),
  проверяем что HTTP sync работает (sent=2 есть? почему?)

Неделя 3: Отладка по реальным данным 10 машин
  □ Анализ LayerError логов: какие слои нестабильны, на каких машинах
  □ Проверка drift_ms по heartbeat: расхождение server_ts между машинами в норме?
  □ Проверка полноты данных: нет ли пропусков в sequence_idx
  □ Проверка HTTP sync: sent=2 (failed) есть? почему?
  □ Исправление найденных проблем, hotfix деплой через GitHub Release

Неделя 4: Processing pipeline — OCR + UI Detection
  □ stage2_ocr.py (PaddleOCR, офлайн)
  □ stage3_ui_detection.py (YOLOv8 pretrained)
  □ stage4_screen_diff.py (screen_change_score, element delta)
  □ confidence_scorer.py (weighted sum formula)
  □ stage6_event_extraction.py (rules-only)

Неделя 5: Vision LLM + second pass
  □ stage5_vision_llm.py (Claude API батч 20)
  □ stage7_second_pass.py (dedup, merge, case_id cascade)
  □ run_pipeline.py (координатор, cron 02:00)

Неделя 6: Analytics
  □ reconstruct_tasks.py
  □ process_mining.py (pm4py)
  □ fte_analysis.py
  □ Jupyter ноутбук

Неделя 7+: Production
  □ WiX MSI + EV Code Signing
  □ Autodistill авторазметка для YOLO на реальных скриншотах пилота
  □ Дообучение YOLOv8 на корпоративных UI
```
