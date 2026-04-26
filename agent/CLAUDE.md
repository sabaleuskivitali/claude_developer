# Seamlean Agent — C# Windows Client

> **НАЗВАНИЕ СЕССИИ:** всегда начинай с `agent: ` — например `agent: Fix window layer stuck`
> Используй `/rename agent: [описание]` в начале каждой сессии.

> **РЕЛИЗ:** если пользователь написал «релизь» (с аргументом или без) — немедленно выполни:
> `~/Applications/Claude/Dev/release agent "[аргумент если есть]"`
> Никаких git команд вручную. Только через этот скрипт.

> Ты работаешь ТОЛЬКО в папке `agent/`. Не трогай `server/` и `cloud/`.
> Корневой CLAUDE.md содержит общие правила проекта (git, версионирование, no hardcode).

---

## Компонент

**Seamlean.Agent.exe** — единый Windows exe, C# .NET 8.
Запускается как Scheduled Task (AtLogOn, BUILTIN\Users, LeastPrivilege).
НЕ Windows Service — Scheduled Task нужен для доступа к пользовательской сессии (скриншоты работают на доменных машинах).

```
agent/
  src/Seamlean.Agent/     ← основной код
  browser-extension/      ← Chrome/Edge Manifest V3
  installer/              ← скрипты установки
  tools/                  ← вспомогательные утилиты
  Makefile
  VERSION
```

---

## Режимы запуска (один exe)

```
Seamlean.Agent.exe --install [--install-code CODE]   → Installer mode
Seamlean.Agent.exe --uninstall [--purge]             → Uninstaller mode
Seamlean.Agent.exe (stdin pipe от браузера)          → Native Messaging Host mode
Seamlean.Agent.exe (no args)                         → Main Service mode
```

---

## Архитектура: слои (IHostedService, параллельные, независимые)

```
Слой A — Window (всегда работает)
  WindowWatcher       WinEventHook EVENT_SYSTEM_FOREGROUND
  IdleDetector        GetLastInputInfo, пороги 30s / 120s
  ClipboardMonitor    WM_CLIPBOARDUPDATE, только метаданные

Слой B — Visual (главный источник контекста)
  ScreenshotWorker    GDI+, WebP quality=80, dHash dedup
                      Триггеры: window_activated, ui_event, periodic 10s, baseline 60s
                      Только активное окно (не весь экран)

Слой C — System
  ProcessWatcher      WMI Win32_Process start/stop
  FileEventCapture    FileSystemWatcher, офисные расширения

Слой D — App Logs (discovery-based)
  AppLogScannerHost   координатор
  EventLogWatcher     Windows Event Log, все каналы
  RegistryMruReader   polling 30s, HKCU MRU → case_id
  LnkWatcher         Recent\ папка, LNK parsing
  FileLogScanner      auto-discovery + delta read

Слой E — Browser
  BrowserQueueImporter  читает JSONL из native messaging
  ExtensionHostService  localhost HTTP для деплоя extension без Store

UI Automation (поверх слоёв)
  UiAutomationCapture   Invoked, ValueChanged, SelectionChanged, ErrorDialogAppeared
                        Значения: только тип + длина + hash, без raw текста
                        Password fields: IsPasswordField=true, скриншот не делается

Инфраструктура
  EventStore          SQLite WAL, один connection, thread-safe lock
  HttpSyncWorker      батчи 100 событий, каждые 30s, POST /api/v1/events
  NtpSynchronizer     каждые 2 мин, медиана 5 запросов, drift_rate_ppm интерполяция
  HeartbeatWorker     каждые 60s, layer stats + NTP drift
  PerformanceMonitor  каждые 5 мин, CPU/RAM/DB size
  HttpCommandPoller   polling /api/v1/commands каждые 60s
  HttpUpdateManager   OTA обновления через /api/v1/updates/latest
  LayerWatchdog       мониторинг зависших слоёв, перезапуск
  LayerHealthTracker  events/errors per layer, last event timestamp
```

---

## Главный принцип данных

**Скриншот — единственный гарантированный источник понимания.**
UIAutomation часто возвращает пустые поля в корпоративных приложениях (1С, SAP, WinForms). Это нормально.

```
Иерархия надёжности:
1. Скриншот + Vision (сервер) — всегда даёт task_label
2. WindowTitle + ProcessName — всегда доступны
3. UIAutomation события — работает для стандартных контролов
4. ETW / WMI события — ProcessStart/Stop, File I/O
```

---

## Хранение

```
SQLite:       %ProgramData%\Microsoft\Diagnostics\events.db
Screenshots:  %ProgramData%\Microsoft\Diagnostics\cache\{YYYYMMDD}\{event_id}.webp
Logs:         %ProgramData%\Microsoft\Diagnostics\logs\agent-*.log (10MB rolling, 5 files)
Settings:     %ProgramFiles%\Windows Diagnostics\appsettings.json
```

---

## Privacy правила (в агенте)

- Поля форм: только тип + длина + SHA256 hash, никакого raw текста
- Password fields: IsPasswordField=true, скриншот null, hash null
- Логи: строки с password|passwd|secret|token|credential|пароль → не записываем
- Clipboard: только метаданные (cross-app флаг), без содержимого
- URLs: query params с token|key|secret|auth|session|password → вырезаем regex
- Заголовки окон: пишутся raw (TODO: маскировка PII — в бэклоге)

---

## Сборка и CI

```bash
dotnet publish --self-contained -p:PublishSingleFile=true -r win-x64
```

CI: `.github/workflows/build-agent.yml`
Артефакт: `Seamlean.Agent.exe` → GitHub Release (не zip)
Тег: `agent/vX.Y.Z`

---

## Контракты с сервером (НЕ менять без согласования с server/)

```
POST /api/v1/events          — батч событий, схема ActivityEvent
POST /api/v1/screenshots/... — WebP файл
POST /api/v1/enroll          — enrollment
GET  /api/v1/commands/{id}   — polling команд
GET  /api/v1/updates/latest  — проверка обновлений
GET  /api/v1/policy/{id}     — whitelist приложений (TODO)

EventType enum               — добавлять можно, удалять/переименовывать нельзя
ActivityEvent schema         — поля только добавлять, не удалять
```

---

## Что сейчас в бэклоге для агента

```
Приоритет 1: Policy/Whitelist
  AgentPolicy: {app_whitelist, domain_whitelist, never_collect}
  HttpCommandPoller получает policy → ScreenshotWorker + UiAutomationCapture проверяют

Приоритет 2: Window Title Privacy
  Маскировка PII в заголовках окон перед отправкой

Приоритет 3: Resource Governor
  Шаг 1 — ResourceGovernor.cs (новый BackgroundService)
    Цикл каждые 5 секунд
    CPU: proc.Refresh() → delta(TotalProcessorTime) / (elapsed × ProcessorCount) × 100
    RAM: GlobalMemoryStatusEx P/Invoke → dwMemoryLoad (0–100%)
    Гистерезис: 2 сэмпла подряд для входа/выхода из режима (избегает дёрганья)
    Публикует: static bool IsThrottled (CPU), static bool IsPaused (RAM)

  Шаг 2 — AgentSettings.cs
    CpuThrottlePercent = 5.0   (Microsoft Defender min = 5%, практика: 3–10%)
    MemLoadPausePercent = 85   (dwMemoryLoad > 85% → 15% свободно, работает на 4/8/16 ГБ)
    ThrottleConsecutiveSamples = 2

  Шаг 3 — ScreenshotWorker.cs
    При IsPaused → пропускать все capture (periodic, baseline, triggered)

  Шаг 4 — UiAutomationCapture.cs
    При IsThrottled → пропускать TriggerCapture (само событие пишем, скриншот нет)

  Шаг 5 — PerformanceMonitor.cs
    Добавить в payload: cpu_pct, mem_load_pct, cpu_throttled, mem_paused

  Почему такие пороги:
    CPU 5% с гистерезисом: непрерывный агент, не пакетный сканер; Citrix WEM режет на 8–15%
    RAM через dwMemoryLoad: фиксированный MB некорректен (500 МБ = 12% на 4ГБ, но 6% на 8ГБ)
    GlobalMemoryStatusEx вместо PerformanceCounter: нет PDH-инициализации (~400мс), нет привилегий

Приоритет 4: Hotkey Detector
  Global keyboard hook для Ctrl+S, Ctrl+Z, F5, Alt+Tab

Приоритет 5: Tray UI (--tray mode)
  Иконка в трее + диалог "какую задачу выполняете?"
  Читает SQLite readonly, пишет task confirmations
```
