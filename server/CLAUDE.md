# Seamlean Server — On-Prem Python Backend

> **НАЗВАНИЕ СЕССИИ:** всегда начинай с `server: ` — например `server: Add analytics worker`
> Используй `/rename server: [описание]` в начале каждой сессии.

> **РЕЛИЗ:** если пользователь написал «релизь» (с аргументом или без) — немедленно выполни:
> `~/Applications/Claude/Dev/release server "[аргумент если есть]"`
> Никаких git команд вручную. Только через этот скрипт.


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

## Контейнеры (docker-compose.yml)

```yaml
services:
  api:      uvicorn api.main:app  — HTTP приём событий от агентов
  worker:   sleep infinity         — analytics worker, запускается через ofelia
  ofelia:   mcuadros/ofelia        — cron планировщик через docker exec
  postgres: PostgreSQL 16
  minio:    MinIO (скриншоты WebP)
  nginx:    :443 → :49200
```

**api** — тонкий HTTP слой, только приём и отдача данных, < 100ms response.
**worker** — `sleep infinity`; pipeline запускается Ofelia через `docker exec` по расписанию.
Так worker всегда доступен для `docker exec` и не падает от OOM при будущих PaddleOCR/YOLO.

**Расписание Ofelia:**
- `0 */2 * * *` — `run_pipeline.py vision` (каждые 2 часа)
- `30 */2 * * *` — `run_pipeline.py reconstruct` (через 30 мин после vision)
- `0 3 * * *` — `run_pipeline.py fte` (ночной FTE отчёт)

**Запустить pipeline вручную:**
```bash
docker compose exec worker python run_pipeline.py all
docker compose exec worker python run_pipeline.py vision --batch-size 5
docker compose exec worker python run_pipeline.py reconstruct --lookback-hours 72
```

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

## Пайплайн аналитики (analytics/)

```
vision_worker.py     — Claude Vision API, батч 20 скриншотов
                       dHash фильтр дублей → MinIO → Claude API → vision_results
                       → materialize в events.vision_*

task_reconstructor.py — сегментация vision-событий в task_sessions
                        граница = commit+idle/gap>3min, или gap>15min, или смена сессии
                        case_id scoring: vision(10) > regex(7) > MRU(4)

fte_builder.py       — агрегация task_sessions → fte_report
                        automation_score (min-max по всем задачам)
                        FTE stays on-prem (privacy)

run_pipeline.py      — координатор: vision / reconstruct / fte / all
```

**MVP реализован**: Vision + task reconstruction + FTE.
**Бэклог**: OCR (PaddleOCR), UI Detection (YOLOv8), Process Mining (pm4py).

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
Приоритет 1: Policy Endpoint
  GET /api/v1/policy/{machine_id}
  Возвращает: {app_whitelist, domain_whitelist, never_collect}
  Хранится в PostgreSQL, редактируется через cloud cabinet

Приоритет 2: OCR + UI Detection (analytics/)
  stage2_ocr.py       — PaddleOCR (офлайн, bbox + confidence)
  stage3_ui.py        — YOLOv8 UI элементы
  Two-tier Vision: OCR+YOLO confidence >= 0.75 → без Claude API (~70% скриншотов)

Приоритет 3: Process Mining
  pm4py discovery → process graph → BPMN
  Conformance checking → отклонения от нормы

Приоритет 4: FTE API для cloud cabinet
  GET /api/v1/fte/{server_id} → агрегированные данные fte_report
  Cloud показывает FTE таблицу клиенту, raw данные остаются on-prem
```

---

## Деплой

```bash
# На сервере клиента:
git pull && docker compose up -d

# Тег: server/vX.Y.Z
# CI: .github/workflows/deploy-server.yml
```
