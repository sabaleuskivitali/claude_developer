# Seamlean Cloud — SaaS Manager Cabinet

> **НАЗВАНИЕ СЕССИИ:** всегда начинай с `cloud: ` — например `cloud: Add policy editor UI`
> Используй `/rename cloud: [описание]` в начале каждой сессии.

> **РЕЛИЗ:** если пользователь написал «релизь» (с аргументом или без) — немедленно выполни:
> `~/Applications/Claude/Dev/release cloud "[аргумент если есть]"`
> Никаких git команд вручную. Только через этот скрипт.


> Ты работаешь ТОЛЬКО в папке `cloud/`. Не трогай `agent/` и `server/`.
> Корневой CLAUDE.md содержит общие правила проекта (git, версионирование, no hardcode).

---

## Компонент

**SaaS кабинет** — размещён на облачном сервере Vitali.
Точки входа: `seamlean.com` (web UI) и `api.seamlean.com` (API).
Управляет регистрацией серверов, агентов, install codes, кабинетом менеджера.

```
cloud/
  main.py              ← FastAPI приложение (всё в одном файле, так исторически)
  docker-compose.yml
  cloudflared-config.yml
  nginx.conf
  Dockerfile
  VERSION
```

---

## Стек

| Компонент | Технология |
|---|---|
| HTTP | FastAPI + uvicorn |
| БД | SQLite (один файл, простота деплоя) |
| Туннель | cloudflared → seamlean.com / api.seamlean.com |
| Реверс-прокси | nginx |
| Хостинг агента | GitHub Releases (302 redirect) |

---

## Домены и маршруты

```
seamlean.com         → web UI, /install.sh, /i/{code}, /cabinet/*
api.seamlean.com     → CNAME → seamlean.com, все /v1/* эндпоинты

CF Tunnel → nginx:443 → uvicorn
```

---

## API эндпоинты

### Для on-prem серверов (install.sh)
```
POST /v1/register-server     ← регистрация нового on-prem сервера
                               ← api_key, tunnel_token, tunnel_url (srv-XXX.seamlean.com)
POST /v1/server-heartbeat    ← heartbeat от on-prem сервера
```

### Bootstrap для агентов
```
POST /v1/installer/bootstrap  ← агент присылает install_code
                               → SignedBootstrapProfile + install_config
GET  /v1/bootstrap/{token}    ← alias для совместимости
```

### Кабинет менеджера
```
GET  /cabinet/               ← главная страница кабинета
GET  /cabinet/servers        ← список серверов тенанта
POST /cabinet/generate-install-code  ← новый install code (TTL 72h, max 999 машин)
GET  /i/{code}               ← страница установки агента (HTML)
GET  /agent                  → 302 → GitHub Release URL (Seamlean.Agent.exe)
```

### Для on-prem серверов (проксирование bootstrap)
```
GET  /api/v1/health          ← cloud проверяет on-prem сервер через tunnel_url
GET  /api/v1/bootstrap/active ← cloud получает SignedBootstrapProfile с on-prem
```

---

## Bootstrap pipeline (полный флоу)

```
1. Менеджер регистрируется → аккаунт в cloud

2. install.sh | sudo bash -- --token INSTALL_TOKEN
   → POST api.seamlean.com/v1/register-server
   ← api_key, tunnel_token, tunnel_url (srv-XXX.seamlean.com)
   → docker compose up + cloudflared на on-prem сервере
   → cloud авто-генерирует install_code для user_id

3. Кабинет показывает:
   Seamlean.Agent.exe --install --install-code CODE
   или ссылку: seamlean.com/i/{code}

4. Агент: POST api.seamlean.com/v1/installer/bootstrap {install_code}
   1. Проверить code (TTL, max_uses, rate_limit)
   2. Получить tunnel_url сервера тенанта
   3. GET tunnel_url/api/v1/bootstrap/active → SignedBootstrapProfile
   4. Re-sign cloud CA ключом
   5. used_count += 1
   ← {profile: SignedBootstrapProfile, install_config: {...}}

5. Агент: POST srv-XXX.seamlean.com/api/v1/enroll
   ← api_key (TTL 180д)

6. Агент: POST /api/v1/events каждые ~30с
```

---

## База данных (SQLite)

```sql
-- Основные таблицы
users          (id, email, password_hash, created_at)
servers        (id, user_id, name, api_key, tunnel_token, tunnel_url, status, last_seen)
install_codes  (code TEXT PK, user_id, created_at, expires_at, max_uses=999, used_count, rate_limit=10)
bootstrap_profiles (id, server_id, profile_json, signed_profile, status, created_at)
```

---

## Кабинет UI (текущий вид)

```
━━━ vs-service-01  ● Онлайн · 3м назад  v1.0.23  ✅DB 25MB  Диск: 478GB ━━━
LAN: https://10.8.20.150:49200   WAN: https://195.222.86.67:443

[Скачать Seamlean.Agent.exe]
Команда: Seamlean.Agent.exe --install --install-code CODE
Ссылка:  seamlean.com/i/{code}  (действует до ДАТА)

МАШИНА    АГЕНТ    LAST SEEN  ВЕРСИЯ  СБОР ДАННЫХ  DRIFT
WS-PH-71  ●online  40с назад  1.4.3  ✅ Работает  +493мс
  └ Окна | Скрины | Система | Логи | Браузер: [5мин|1ч|сутки|итого]
```

Статусы агента "Сбор данных":
- ✅ Работает — все активные слои events_1h > 0
- ⚠ Нет событий за час — idle 1ч (но был в сутках)
- ❌ Не работает — ни один слой не дал событий за 24ч
- Неизвестно — агент offline

---

## GitHub Releases интеграция

```python
# cloud/main.py — webhook от GitHub
# При новом Release → обновляет URL в SQLite
# GET /agent → 302 → GitHub Release URL Seamlean.Agent.exe

# GitHub secret: GITHUB_WEBHOOK_SECRET
# Ищет asset: "Seamlean.Agent.exe" (не .zip)
```

---

## ENV переменные

```
SECRET_KEY            ← JWT/signing ключ
CF_API_TOKEN          ← Cloudflare API (создание туннелей)
CF_ACCOUNT_ID         ← Cloudflare account
GITHUB_WEBHOOK_SECRET ← верификация GitHub webhook
```

---

## Деплой

```bash
# На cloud сервере:
git pull && docker compose up -d

# Тег: cloud/vX.Y.Z
# CI: .github/workflows/deploy-cloud.yml
```

---

## Что сейчас в бэклоге для cloud

```
Приоритет 1: Policy Editor в кабинете
  UI для настройки whitelist приложений per server/machine
  POST /cabinet/servers/{id}/policy
  → on-prem сервер забирает через /api/v1/policy

Приоритет 2: Расширение кабинета
  Просмотр задач и процессов (данные с on-prem через API)
  Task Mining результаты
  Automation candidates список

Приоритет 3: Process Map UI
  Визуализация графа процессов из on-prem сервера
  BPMN-light отображение

Приоритет 4: install_code настройки
  max_uses настройка в кабинете (сейчас hardcode 999)
  domain_pattern фильтр для кодов
  GPO: env var SEAMLEAN_INSTALL_CODE для silent deploy
```

---

## Важно: main.py — монолитный файл

Весь cloud — один файл `main.py` (~1500+ строк).
Это исторически сложилось, не рефакторить без явной задачи.
Добавляй эндпоинты в конец файла, не реорганизуй структуру.
