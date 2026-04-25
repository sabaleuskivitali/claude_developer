# Seamlean Server — On-Prem Python Backend

> **НАЗВАНИЕ СЕССИИ:** всегда начинай с `server: ` — например `server: Add analytics worker`
> Используй `/rename server: [описание]` в начале каждой сессии.


> Ты работаешь ТОЛЬКО в папке `server/`. Не трогай `agent/` и `cloud/`.
> Корневой CLAUDE.md содержит общие правила проекта (git, версионирование, no hardcode).

---

## Компонент

**On-prem сервер** — устанавливается в инфраструктуре клиента.
Docker Compose на Ubuntu 22.04 LTS.
Принимает данные от агентов на Windows-машинах через CF Tunnel.

```
server/
  api/           ← FastAPI HTTP сервис (принимает события от агентов)
  analytics/     ← воркер (task mining, LLM, process builder)
  etl/           ← ETL пайплайн (импорт из SQLite агента)
  bootstrap/     ← bootstrap логика
  nginx/         ← nginx конфиг
  schema.sql     ← схема PostgreSQL
  schema_v2.sql  ← текущая схема
  docker-compose.yml
  manage.py
  VERSION
```

---

## Два процесса (один Docker образ, два контейнера)

```yaml
services:
  api:      uvicorn api.main:app --host 0.0.0.0 --port 49200
  worker:   python -m analytics.main
  postgres: PostgreSQL 16
  minio:    MinIO (скриншоты)
  cloudflared: CF Tunnel → srv-XXX.seamlean.com
  nginx:    :443 → :49200, :49100 (discovery)
```

**api** — тонкий HTTP слой, только приём и отдача данных, < 100ms response.
**worker** — тяжёлая аналитика, может работать секунды и минуты.

---

## Стек

| Компонент | Технология |
|---|---|
| HTTP API | FastAPI + uvicorn |
| БД | PostgreSQL 16 |
| File storage | MinIO (скриншоты WebP) |
| Очередь задач | PostgreSQL queue (→ Redis когда нужно) |
| OCR | PaddleOCR (офлайн, bbox + confidence) |
| UI Detection | YOLOv8 + Autodistill |
| Vision LLM | Claude API батч 20 скриншотов (MVP) |
| Process Mining | pm4py |
| FTE-анализ | pandas + scipy |
| Туннель | cloudflared → srv-XXX.seamlean.com |

---

## API эндпоинты (принимает от агентов)

```
POST /api/v1/events              ← батч событий от агента (100 шт.)
POST /api/v1/screenshots/{...}   ← WebP файл скриншота
POST /api/v1/enroll              ← регистрация нового агента
POST /api/v1/heartbeat           ← пульс агента
GET  /api/v1/commands/{machine_id} ← команды для агента (polling)
GET  /api/v1/updates/latest      ← проверка версии агента
GET  /api/v1/policy/{machine_id} ← whitelist приложений для агента (TODO)
GET  /api/v1/health              ← health check (cloud проверяет)
GET  /api/v1/bootstrap/active    ← bootstrap profile для агентов
```

---

## Пайплайн аналитики (worker)

```
Stage 1: Import
  Агент → POST /api/v1/events → PostgreSQL (events table)
  Агент → POST /api/v1/screenshots → MinIO

Stage 2: OCR
  Скриншот из MinIO → PaddleOCR → текст + bbox → ocr_results table

Stage 3: UI Detection
  Скриншот → YOLOv8 → UI элементы → ui_elements table

Stage 4: Screen Diff
  Сравнение последовательных скриншотов → delta → что изменилось

Stage 5: Vision LLM (главный)
  Скриншот + контекст → Claude API (батч 20) → task_label, app_context, action
  Результат → vision_results table

Stage 6: Event Extraction
  События + Vision результаты → Python rules + LLM → structured events

Stage 7: Validation
  Dedup, merge, case_id repair, confidence recalculation

Stage 8: Process Mining
  pm4py discovery → process graph → BPMN
  Conformance checking → отклонения от нормы
```

**MVP**: Stages 1 + 5 (Import + Vision LLM). Остальные добавляются итерационно.

---

## Модель событий (от агента)

```python
# events table (PostgreSQL)
event_id        UUID PRIMARY KEY
session_id      UUID
machine_id      TEXT
user_id         TEXT
timestamp_utc   TIMESTAMPTZ
drift_ms        INTEGER          # NTP коррекция
drift_rate_ppm  FLOAT
sequence_idx    INTEGER          # монотонный per session
layer           TEXT             # A/B/C/D/E
event_type      TEXT             # WindowActivated, Screenshot, etc.
process_name    TEXT
window_title    TEXT
element_type    TEXT
element_name    TEXT
screenshot_path TEXT             # relative path в MinIO
raw_message     TEXT             # логи, до 500 символов
payload         JSONB            # всё остальное
sent_at         TIMESTAMPTZ
```

---

## Портирование данных из SQLite агента

```
etl/
  import_sqlite.py    ← импорт из agent SQLite в PostgreSQL
  migrate.py          ← DB миграции
```

Агент пишет в SQLite (offline buffer), sync через HTTP батчи.
Сервер принимает батчи → INSERT в PostgreSQL.

---

## Контракты с агентом (НЕ менять без согласования с agent/)

```
POST /api/v1/events — схема ActivityEvent
  Поля только добавлять, не удалять/переименовывать
  
EventType значения — агент их отправляет, сервер хранит
  Добавлять можно, удалять нельзя без версионирования
```

---

## Контракты с cloud (НЕ менять без согласования с cloud/)

```
GET  /api/v1/health          ← cloud проверяет статус сервера
GET  /api/v1/bootstrap/active ← cloud проксирует агентам
POST /api/v1/register        ← cloud регистрирует сервер (при install.sh)

Heartbeat формат → cloud dashboard
```

---

## Что сейчас в бэклоге для сервера

```
Приоритет 1: Analytics Worker (базовый)
  Task Miner: группировка событий в задачи по сессии
  LLM Processor: Claude API батч скриншотов → task_label
  PostgreSQL queue: events WHERE processed=false

Приоритет 2: Policy Endpoint
  GET /api/v1/policy/{machine_id}
  Возвращает: {app_whitelist, domain_whitelist, never_collect}
  Хранится в PostgreSQL, редактируется через cloud cabinet

Приоритет 3: Process Builder
  pm4py на данных task mining
  Граф процессов → PostgreSQL

Приоритет 4: Confidence Scorer
  Оценка паттернов по частоте + подтверждениям

Приоритет 5: Chat/Notification
  Telegram Bot интеграция
  Уведомления сотруднику о задачах-кандидатах
```

---

## Деплой

```bash
# На сервере клиента:
git pull && docker compose up -d

# Тег: server/vX.Y.Z
# CI: .github/workflows/deploy-server.yml
```
