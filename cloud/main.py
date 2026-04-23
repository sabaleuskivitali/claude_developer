import hashlib
import json
import os
import re
import sqlite3
import ssl
import urllib.request
import uuid
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

DB_PATH = Path("/app/data/users.db")

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        pwd_hash TEXT NOT NULL,
        install_token TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS servers (
        user_id TEXT PRIMARY KEY,
        server_url TEXT NOT NULL,
        api_key TEXT NOT NULL,
        server_name TEXT DEFAULT 'Мой сервер',
        registered_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.commit()
    # migrate: add install_token if column missing
    try:
        conn.execute("ALTER TABLE users ADD COLUMN install_token TEXT")
        conn.commit()
    except Exception:
        pass
    conn.close()


init_db()


# ── DB helpers ────────────────────────────────────────────────────────────────

def _db_user_by_session(token: str):
    if not token:
        return None
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT u.id, u.email FROM users u JOIN sessions s ON u.id=s.user_id WHERE s.token=?",
        (token,)
    ).fetchone()
    conn.close()
    return {"id": row[0], "email": row[1]} if row else None


def _hash(pwd: str) -> str:
    return hashlib.sha256(pwd.encode()).hexdigest()


def _get_install_token(user_id: str) -> str:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT install_token FROM users WHERE id=?", (user_id,)).fetchone()
    if row and row[0]:
        conn.close()
        return row[0]
    token = uuid.uuid4().hex
    conn.execute("UPDATE users SET install_token=? WHERE id=?", (token, user_id))
    conn.commit()
    conn.close()
    return token


def _get_user_server(user_id: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT server_url, api_key, server_name FROM servers WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    return {"server_url": row[0], "api_key": row[1], "server_name": row[2]} if row else None


# ── API helpers ───────────────────────────────────────────────────────────────

def _api(server_url: str, api_key: str, path: str):
    try:
        req = urllib.request.Request(
            f"{server_url.rstrip('/')}{path}",
            headers={"X-Api-Key": api_key, "User-Agent": "Seamlean-Cloud/1.0"},
        )
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return None


# ── HTML ──────────────────────────────────────────────────────────────────────

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;color:#222;min-height:100vh}
nav{background:#fff;border-bottom:1px solid #e5e7eb;padding:0 24px;height:52px;display:flex;align-items:center;justify-content:space-between}
.nav-brand{font-weight:700;font-size:1.05rem;color:#111;text-decoration:none}
.nav-right{display:flex;align-items:center;gap:12px;font-size:.85rem;color:#666}
.wrap{max-width:480px;margin:60px auto;padding:0 16px}
.wrap-wide{max-width:860px;margin:40px auto;padding:0 20px}
.card{background:#fff;border-radius:12px;padding:32px;box-shadow:0 2px 12px rgba(0,0,0,.07);margin-bottom:20px}
h1{font-size:1.5rem;font-weight:700;margin-bottom:6px}
.sub{color:#666;font-size:.9rem;margin-bottom:22px}
label{display:block;font-size:.84rem;font-weight:600;margin-bottom:4px;color:#444}
input[type=email],input[type=password]{width:100%;padding:10px 12px;border:1.5px solid #ddd;border-radius:8px;font-size:1rem;outline:none;transition:border .15s}
input:focus{border-color:#4f46e5}
.btn{display:inline-block;padding:10px 20px;background:#4f46e5;color:#fff;border:none;border-radius:8px;font-size:.94rem;font-weight:600;cursor:pointer;text-decoration:none;transition:opacity .15s}
.btn:hover{opacity:.88}
.btn-block{display:block;width:100%;text-align:center;margin-top:16px}
.btn-gray{background:#e5e7eb;color:#374151}
.link{text-align:center;margin-top:14px;font-size:.88rem;color:#666}
.link a{color:#4f46e5;text-decoration:none;font-weight:600}
.err{color:#dc2626;font-size:.84rem;padding:8px 12px;background:#fef2f2;border-radius:6px;margin-top:8px}
.badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:.76rem;font-weight:700}
.online{background:#d1fae5;color:#065f46}
.warning{background:#fef3c7;color:#92400e}
.offline{background:#fee2e2;color:#991b1b}
.sec-title{font-size:.95rem;font-weight:700;color:#374151;margin-bottom:10px}
table{width:100%;border-collapse:collapse;font-size:.86rem}
th{background:#f9fafb;padding:7px 10px;text-align:left;font-weight:600;color:#6b7280;font-size:.76rem;text-transform:uppercase;letter-spacing:.04em;border-bottom:1px solid #e5e7eb}
td{padding:8px 10px;border-bottom:1px solid #f3f4f6;vertical-align:middle}
tr:last-child td{border-bottom:none}
.code-box{background:#f3f4f6;border-radius:8px;padding:9px 12px;font-family:monospace;font-size:.82rem;word-break:break-all}
.copy-row{display:flex;gap:8px;align-items:center;margin:8px 0}
.copy-row .code-box{flex:1}
.tabs{display:flex;border-bottom:2px solid #e5e7eb;margin-bottom:24px}
.tab{padding:8px 18px;cursor:pointer;font-size:.92rem;font-weight:500;color:#6b7280;background:none;border:none;border-bottom:2px solid transparent;margin-bottom:-2px;transition:color .15s}
.tab.active{color:#4f46e5;border-bottom-color:#4f46e5;font-weight:700}
.panel{display:none}.panel.active{display:block}
.hero{text-align:center;padding:5rem 1rem 3rem}
.hero h1{font-size:2.4rem;font-weight:700;line-height:1.2;margin-bottom:1rem}
.hero p{font-size:1.05rem;color:#555;max-width:500px;margin:0 auto 2rem}
.hero-btns{display:flex;gap:1rem;justify-content:center;flex-wrap:wrap}
.features{display:grid;grid-template-columns:repeat(3,1fr);gap:1.5rem;max-width:820px;margin:0 auto;padding:0 1rem 4rem}
.feature{background:#fff;border-radius:12px;padding:1.4rem;box-shadow:0 1px 8px rgba(0,0,0,.06)}
.feature-icon{font-size:1.4rem;margin-bottom:.6rem}
.feature-title{font-weight:600;margin-bottom:.4rem}
.feature-text{font-size:.86rem;color:#666}
.hint{font-size:.78rem;color:#9ca3af;margin-top:6px}
"""

_JS = """
function copyText(id){
  var el=document.getElementById(id);
  navigator.clipboard.writeText(el.innerText).then(function(){
    var b=document.getElementById(id+'-btn');
    var orig=b.textContent;b.textContent='Скопировано!';
    setTimeout(function(){b.textContent=orig},2000);
  });
}
function showTab(name){
  document.querySelectorAll('.tab').forEach(function(t){t.classList.toggle('active',t.dataset.tab===name)});
  document.querySelectorAll('.panel').forEach(function(p){p.classList.toggle('active',p.id==='panel-'+name)});
}
"""


def _page(title: str, nav_right: str, body: str, wide: bool = False) -> HTMLResponse:
    wrap = "wrap-wide" if wide else "wrap"
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>{_CSS}</style>
</head>
<body>
<nav>
  <a class="nav-brand" href="/">Seamlean</a>
  <div class="nav-right">{nav_right}</div>
</nav>
<div class="{wrap}">{body}</div>
<script>{_JS}</script>
</body>
</html>""")


# ── Landing ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    token = request.cookies.get("session")
    if _db_user_by_session(token):
        return RedirectResponse("/cabinet", status_code=302)
    nav = '<a href="/login" class="btn btn-gray" style="padding:6px 16px">Войти</a>'
    body = """
<div class="hero" style="margin:-40px -20px 0">
  <h1>Узнайте, что реально<br>делают сотрудники</h1>
  <p>Seamlean собирает действия на рабочих компьютерах и строит карту процессов —
     с рекомендациями где автоматизировать через RPA или ИИ-агента.</p>
  <div class="hero-btns">
    <a href="/register" class="btn" style="font-size:1rem;padding:.75rem 2rem">Начать бесплатно</a>
    <a href="/login" class="btn btn-gray" style="font-size:1rem;padding:.75rem 2rem">Войти</a>
  </div>
</div>
<div class="features" style="margin-top:3rem">
  <div class="feature">
    <div class="feature-icon">🖥</div>
    <div class="feature-title">Автоматический сбор</div>
    <div class="feature-text">Агент работает в фоне, не мешает пользователю. Скриншоты, UIAutomation, системные события.</div>
  </div>
  <div class="feature">
    <div class="feature-icon">🔍</div>
    <div class="feature-title">Анализ Vision AI</div>
    <div class="feature-text">Claude анализирует скриншоты и выявляет задачи, кейсы, паттерны поведения.</div>
  </div>
  <div class="feature">
    <div class="feature-icon">📊</div>
    <div class="feature-title">FTE-таблица</div>
    <div class="feature-text">Рекомендации по автоматизации: RPA, гибрид или ИИ-агент — для каждого процесса.</div>
  </div>
</div>"""
    return _page("Seamlean — анализ рабочих процессов", nav, body, wide=True)


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def login_page(err: str = ""):
    err_html = f'<div class="err">{err}</div>' if err else ""
    body = f"""
<div class="card">
  <h1>Войти</h1>
  <p class="sub">Личный кабинет Seamlean</p>
  {err_html}
  <form method="post" action="/login">
    <label>Email</label>
    <input type="email" name="email" required autofocus placeholder="you@company.com">
    <label style="margin-top:14px">Пароль</label>
    <input type="password" name="password" required placeholder="••••••••">
    <button class="btn btn-block" type="submit">Войти</button>
  </form>
  <p class="link"><a href="/register">Нет аккаунта? Зарегистрироваться</a></p>
</div>"""
    return _page("Войти — Seamlean", "", body)


@app.post("/login", response_class=HTMLResponse)
def login_post(email: str = Form(...), password: str = Form(...)):
    conn = sqlite3.connect(DB_PATH)
    row  = conn.execute(
        "SELECT id FROM users WHERE email=? AND pwd_hash=?",
        (email.lower().strip(), _hash(password))
    ).fetchone()
    conn.close()
    if not row:
        return RedirectResponse("/login?err=Неверный+email+или+пароль", status_code=302)
    token = str(uuid.uuid4())
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO sessions(token,user_id) VALUES(?,?)", (token, row[0]))
    conn.commit()
    conn.close()
    resp = RedirectResponse("/cabinet", status_code=302)
    resp.set_cookie("session", token, httponly=True, samesite="lax")
    return resp


@app.get("/register", response_class=HTMLResponse)
def register_page(err: str = ""):
    err_html = f'<div class="err">{err}</div>' if err else ""
    body = f"""
<div class="card">
  <h1>Создать аккаунт</h1>
  <p class="sub">Бесплатно, без карты</p>
  {err_html}
  <form method="post" action="/register">
    <label>Email</label>
    <input type="email" name="email" required autofocus placeholder="you@company.com">
    <label style="margin-top:14px">Пароль</label>
    <input type="password" name="password" required minlength="6" placeholder="Минимум 6 символов">
    <button class="btn btn-block" type="submit">Зарегистрироваться</button>
  </form>
  <p class="link"><a href="/login">Уже есть аккаунт? Войти</a></p>
</div>"""
    return _page("Регистрация — Seamlean", "", body)


@app.post("/register", response_class=HTMLResponse)
def register_post(email: str = Form(...), password: str = Form(...)):
    email = email.lower().strip()
    if not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
        return RedirectResponse("/register?err=Некорректный+email", status_code=302)
    if len(password) < 6:
        return RedirectResponse("/register?err=Пароль+минимум+6+символов", status_code=302)
    uid = str(uuid.uuid4())
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO users(id,email,pwd_hash) VALUES(?,?,?)", (uid, email, _hash(password)))
        conn.commit()
        conn.close()
    except sqlite3.IntegrityError:
        return RedirectResponse("/register?err=Email+уже+зарегистрирован", status_code=302)
    token = str(uuid.uuid4())
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO sessions(token,user_id) VALUES(?,?)", (token, uid))
    conn.commit()
    conn.close()
    resp = RedirectResponse("/cabinet", status_code=302)
    resp.set_cookie("session", token, httponly=True, samesite="lax")
    return resp


@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get("session")
    if token:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        conn.close()
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("session")
    return resp


# ── Server registration (called by install.sh) ────────────────────────────────

@app.post("/api/register-server")
async def register_server(request: Request):
    try:
        data = await request.json()
        install_token = data.get("token", "")
        server_url    = data.get("server_url", "").rstrip("/")
        api_key       = data.get("api_key", "")
        server_name   = data.get("server_name", "Мой сервер")
        if not all([install_token, server_url, api_key]):
            return {"ok": False, "error": "Missing fields"}
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT id FROM users WHERE install_token=?", (install_token,)).fetchone()
        if not row:
            conn.close()
            return {"ok": False, "error": "Invalid token"}
        conn.execute(
            "INSERT OR REPLACE INTO servers(user_id,server_url,api_key,server_name) VALUES(?,?,?,?)",
            (row[0], server_url, api_key, server_name)
        )
        conn.commit()
        conn.close()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Cabinet ───────────────────────────────────────────────────────────────────

_LAYER_LABELS = {"window": "Окна", "visual": "Скрины", "system": "Система",
                 "applogs": "Логи", "browser": "Браузер"}
_ST_ICON = {"online": "🟢", "warning": "🟡", "offline": "🔴"}


def _layers_html(layer_stats) -> str:
    try:
        stats = json.loads(layer_stats) if isinstance(layer_stats, str) else (layer_stats or {})
    except Exception:
        return "—"
    parts = []
    for key, label in _LAYER_LABELS.items():
        ls = stats.get(key)
        if ls is None:
            continue
        err = (ls.get("errors_5min") or 0) > 0
        bg  = "#fee2e2" if err else "#d1fae5"
        col = "#991b1b" if err else "#065f46"
        parts.append(f'<span style="background:{bg};color:{col};padding:1px 5px;border-radius:4px;font-size:.74rem;font-weight:600;margin-right:2px">{label}</span>')
    return "".join(parts) or "—"


def _agent_rows(agents) -> str:
    if not agents:
        return '<tr><td colspan="5" style="text-align:center;color:#9ca3af;padding:20px">Агентов нет. Установите первого с помощью команды выше.</td></tr>'
    rows = ""
    for a in agents:
        lag   = a.get("lag_sec", 0)
        drift = a.get("drift_ms")
        st    = a.get("status", "offline")
        last  = f"{lag}с" if lag < 60 else (f"{lag//60}м" if lag < 3600 else f"{lag//3600}ч")
        drift_s = (f'<span style="color:{"#dc2626" if abs(drift)>1000 else "#374151"}">{drift:+d}мс</span>'
                   if drift is not None else "—")
        rows += f"""<tr>
          <td style="font-family:monospace;font-size:.78rem">{a.get('machine_id','')[:12]}…</td>
          <td><span class="badge {st}">{_ST_ICON.get(st,'❓')} {st}</span></td>
          <td style="color:#6b7280">{last} назад</td>
          <td>{_layers_html(a.get('layer_stats'))}</td>
          <td>{drift_s}</td>
        </tr>"""
    return rows


@app.get("/cabinet", response_class=HTMLResponse)
def cabinet(request: Request):
    token = request.cookies.get("session")
    user  = _db_user_by_session(token)
    if not user:
        return RedirectResponse("/login", status_code=302)

    install_token = _get_install_token(user["id"])
    user_server   = _get_user_server(user["id"])

    # ── Server panel ──────────────────────────────────────────────────────────
    if user_server:
        health = _api(user_server["server_url"], user_server["api_key"], "/health")
        if health and health.get("status") in ("ok", "degraded"):
            status_badge = '<span class="badge online">🟢 Онлайн</span>'
            extra = ""
            if health.get("version"):
                extra += f'<tr><td style="color:#6b7280">Версия</td><td>{health["version"]}</td></tr>'
            if health.get("db"):
                db_ok = health["db"] == "ok"
                db_size = f' — {health["db_size_mb"]} MB' if health.get("db_size_mb") is not None else ""
                extra += f'<tr><td style="color:#6b7280">База данных</td><td>{"✅" if db_ok else "⚠️"} {health["db"]}{db_size}</td></tr>'
            if health.get("disk_free_gb") is not None:
                extra += f'<tr><td style="color:#6b7280">Диск свободно</td><td>{health["disk_free_gb"]} GB</td></tr>'
        else:
            status_badge = '<span class="badge offline">🔴 Недоступен</span>'
            extra = ""

        server_panel = f"""
  <div class="card">
    <div class="sec-title">{user_server['server_name']}</div>
    <table>
      <tr><td style="color:#6b7280;width:160px">Адрес</td><td><code>{user_server['server_url']}</code></td></tr>
      <tr><td style="color:#6b7280">Статус</td><td>{status_badge}</td></tr>
      {extra}
    </table>
  </div>"""
        agents_data   = (_api(user_server["server_url"], user_server["api_key"], "/api/v1/agents") or {}).get("agents", [])
        bootstrap_raw = _api(user_server["server_url"], user_server["api_key"], "/api/v1/bootstrap/active") or {}
        # If the server has an active profile, the bootstrap URL is just the public endpoint
        bootstrap_url = (user_server["server_url"].rstrip("/") + "/api/v1/bootstrap/active"
                         if bootstrap_raw.get("signed_data") else "")
    else:
        install_cmd = f"curl -fsSL https://seamlean.com/install.sh | sudo bash -s -- --token {install_token}"
        server_panel = f"""
  <div class="card">
    <div class="sec-title">Установить сервер</div>
    <p style="font-size:.85rem;color:#6b7280;margin-bottom:10px">Ubuntu 20.04+, 2 CPU, 4 GB RAM, исходящий интернет:</p>
    <div class="copy-row">
      <div class="code-box" id="srv-cmd">{install_cmd}</div>
      <button class="btn btn-gray" id="srv-cmd-btn" style="padding:7px 14px;font-size:.82rem;white-space:nowrap" onclick="copyText('srv-cmd')">Скопировать</button>
    </div>
    <p class="hint">Команда установит Docker, сервер и все сервисы. После завершения сервер появится здесь автоматически.</p>
  </div>"""
        agents_data   = []
        bootstrap_url = ""

    # ── Agents panel ──────────────────────────────────────────────────────────
    online = sum(1 for a in agents_data if a.get("status") == "online")
    agent_badge = (f"&nbsp;<span style='background:#4f46e5;color:#fff;border-radius:20px;"
                   f"padding:1px 7px;font-size:.75rem'>{online}/{len(agents_data)}</span>"
                   if agents_data else "")

    if bootstrap_url:
        proxy_url = f"https://seamlean.com/bootstrap/{install_token}"
        ps_cmd = f"powershell -ExecutionPolicy Bypass -Command \"iwr 'https://seamlean.com/agent' -OutFile $env:TEMP\\\\sl-agent.zip; Expand-Archive -Path $env:TEMP\\\\sl-agent.zip -DestinationPath $env:TEMP\\\\sl-agent -Force; & $env:TEMP\\\\sl-agent\\\\install.ps1 -BootstrapProfileUrl '{proxy_url}'\""
        agent_install_section = f"""
    <div class="sec-title">Установить агент на Windows</div>
    <p style="font-size:.85rem;color:#6b7280;margin-bottom:8px">Запустите на Windows-машине от имени администратора:</p>
    <div class="copy-row">
      <div class="code-box" id="burl">{ps_cmd}</div>
      <button class="btn btn-gray" id="burl-btn" style="padding:7px 14px;font-size:.82rem;white-space:nowrap" onclick="copyText('burl')">Скопировать</button>
    </div>
    <p class="hint">Агент установится тихо, без перезагрузки.</p>"""
    else:
        agent_install_section = '<p style="color:#9ca3af;font-size:.88rem">Сначала установите и запустите сервер — команда установки агента появится здесь.</p>'

    nav = f"""
    <span>{user['email']}</span>
    <form method="post" action="/logout" style="display:inline">
      <button class="btn btn-gray" style="padding:5px 14px;font-size:.82rem">Выйти</button>
    </form>"""

    body = f"""
<div class="tabs">
  <button class="tab active" data-tab="server" onclick="showTab('server')">Сервер</button>
  <button class="tab" data-tab="agents" onclick="showTab('agents')">Агенты{agent_badge}</button>
</div>

<div id="panel-server" class="panel active">
  {server_panel}
</div>

<div id="panel-agents" class="panel">
  <div class="card">
    {agent_install_section}
  </div>
  <div class="card">
    <div class="sec-title">Устройства</div>
    <table>
      <thead><tr>
        <th>Machine ID</th><th>Статус</th><th>Last seen</th><th>Слои</th><th>Drift</th>
      </tr></thead>
      <tbody>{_agent_rows(agents_data)}</tbody>
    </table>
  </div>
</div>"""

    return _page("Кабинет — Seamlean", nav, body, wide=True)


# ── install.sh ────────────────────────────────────────────────────────────────

_INSTALL_SH = r"""#!/usr/bin/env bash
set -euo pipefail

REPO="https://github.com/sabaleuskivitali/claude_developer.git"
INSTALL_DIR="/opt/seamlean"
INSTALL_TOKEN=""
SERVER_NAME="seamlean"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --token|-t) INSTALL_TOKEN="$2"; shift 2;;
    --name|-n)  SERVER_NAME="$2";   shift 2;;
    *) shift;;
  esac
done

echo "=== Seamlean Server Installer ==="

[ "$EUID" -ne 0 ] && { echo "Run as root: sudo bash"; exit 1; }

# ── Dependencies ──────────────────────────────────────────────────────────────
apt-get update -q
apt-get install -y -q curl git openssl avahi-daemon

# ── Docker ────────────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
fi

if ! docker compose version &>/dev/null 2>&1; then
  apt-get install -y -q docker-compose-plugin
fi

# ── Clone / update ────────────────────────────────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
  echo "Updating existing installation..."
  git -C "$INSTALL_DIR" pull --ff-only
else
  echo "Cloning repository..."
  git clone --depth=1 "$REPO" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR/server"

# ── Generate .env ─────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
  PG_PASS=$(openssl rand -hex 16)
  MINIO_KEY=$(openssl rand -hex 12)
  MINIO_SECRET=$(openssl rand -hex 20)
  API_KEY=$(openssl rand -hex 32)
  cat > .env << EOF
SERVER_NAME=${SERVER_NAME}
POSTGRES_DB=diag
POSTGRES_USER=diag
POSTGRES_PASSWORD=${PG_PASS}
API_KEY=${API_KEY}
MINIO_ACCESS_KEY=${MINIO_KEY}
MINIO_SECRET_KEY=${MINIO_SECRET}
PORT_RANGE_START=49200
PORT_RANGE_END=49300
UPDATE_PACKAGES_DIR=/opt/seamlean/updates
SMB_MOUNT_PATH=/mnt/diag
SMB_SHARE_PATH=/mnt/diag
ETL_INTERVAL_MINUTES=60
SERVER_URL=
EOF
  echo "Generated secrets in .env"
fi

# Avahi services directory (mDNS for local discovery)
mkdir -p /etc/avahi/services
mkdir -p /opt/seamlean/updates /mnt/diag

# ── Start ─────────────────────────────────────────────────────────────────────
echo "Starting services..."
docker compose pull -q 2>/dev/null || true
docker compose up -d --build

# ── Wait for API ──────────────────────────────────────────────────────────────
echo -n "Waiting for API"
for i in $(seq 1 30); do
  if curl -sk https://127.0.0.1:49200/health | grep -q '"status"'; then
    echo " ready"
    break
  fi
  echo -n "."
  sleep 2
done

# ── Register with cloud ───────────────────────────────────────────────────────
if [ -n "$INSTALL_TOKEN" ]; then
  # Try public IP first, fall back to local IP
  PUBLIC_IP=$(curl -4 -sf --max-time 5 https://icanhazip.com 2>/dev/null | tr -d '[:space:]')
  if [ -z "$PUBLIC_IP" ]; then
    PUBLIC_IP=$(hostname -I | awk '{print $1}')
  fi
  SERVER_URL="https://${PUBLIC_IP}:49200"
  # Write SERVER_URL to .env so the bootstrap generator can use it
  if ! grep -q '^SERVER_URL=' .env; then
    echo "SERVER_URL=${SERVER_URL}" >> .env
  fi
  API_KEY_VAL=$(grep '^API_KEY=' .env | cut -d= -f2)

  echo "Registering server with Seamlean cloud..."
  RESP=$(curl -sf -X POST "https://seamlean.com/api/register-server" \
    -H "Content-Type: application/json" \
    -d "{\"token\":\"${INSTALL_TOKEN}\",\"server_url\":\"${SERVER_URL}\",\"api_key\":\"${API_KEY_VAL}\",\"server_name\":\"${SERVER_NAME}\"}" \
    2>/dev/null || echo '{"ok":false}')

  if echo "$RESP" | grep -q '"ok":true'; then
    echo "✅ Server registered! Open https://seamlean.com/cabinet to see status."
  else
    echo "⚠️  Registration failed — open cabinet manually and add server URL: $SERVER_URL"
  fi
fi

echo ""
echo "=== ✅ Seamlean server installed ==="
echo "API: https://$(curl -4 -sf --max-time 5 https://icanhazip.com 2>/dev/null | tr -d '[:space:]' || hostname -I | awk '{print $1}'):49200"
echo "Logs: docker compose -f $INSTALL_DIR/server/docker-compose.yml logs -f"
"""


@app.get("/install.sh", response_class=PlainTextResponse)
def install_sh():
    return PlainTextResponse(_INSTALL_SH, media_type="text/x-sh")


# ── Agent package download ────────────────────────────────────────────────────

_AGENT_ZIP = Path("/app/agent/WinDiagSvc.zip")

@app.get("/agent")
def agent_download():
    if _AGENT_ZIP.exists():
        from fastapi.responses import FileResponse
        return FileResponse(_AGENT_ZIP, media_type="application/zip", filename="WinDiagSvc.zip")
    return JSONResponse(status_code=503, content={"error": "Agent package not yet available"})


# ── Bootstrap proxy (hides server URL from agent install command) ─────────────

@app.get("/bootstrap/{token}")
def bootstrap_proxy(token: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT u.id FROM users u WHERE u.install_token = ?", (token,)
    ).fetchone()
    conn.close()
    if not row:
        return JSONResponse(status_code=404, content={"error": "Invalid token"})
    user_server = _get_user_server(row[0])
    if not user_server:
        return JSONResponse(status_code=503, content={"error": "Server not registered"})
    data = _api(user_server["server_url"], user_server["api_key"], "/api/v1/bootstrap/active")
    if not data:
        return JSONResponse(status_code=503, content={"error": "Bootstrap profile unavailable"})
    return data


# ── Robots / Health ───────────────────────────────────────────────────────────

@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nAllow: /\n"


@app.get("/health")
def health():
    return {"status": "ok"}
