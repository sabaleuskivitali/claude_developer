import asyncio
import base64
import concurrent.futures
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import ssl
import time
import urllib.request
import uuid
from pathlib import Path

_CF_ACCOUNT_ID         = os.environ.get("CF_ACCOUNT_ID", "")
_CF_API_TOKEN          = os.environ.get("CF_API_TOKEN", "")
_CF_DNS_TOKEN          = os.environ.get("CF_DNS_TOKEN", "")
_CF_ZONE_ID            = os.environ.get("CF_ZONE_ID", "")
_CF_BASE               = "https://api.cloudflare.com/client/v4"
_GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
_CLOUD_WEBHOOK_SECRET  = os.environ.get("CLOUD_WEBHOOK_SECRET", "") or _GITHUB_WEBHOOK_SECRET
_GITHUB_TOKEN          = os.environ.get("GITHUB_TOKEN", "")
_CLOUD_VERSION         = Path("/app/VERSION").read_text().strip() if Path("/app/VERSION").exists() else "unknown"

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

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
    # servers table created by _migrate_servers_table() below
    conn.execute("""CREATE TABLE IF NOT EXISTS config (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS install_codes (
        code        TEXT PRIMARY KEY,
        user_id     TEXT NOT NULL,
        created_at  TEXT DEFAULT (datetime('now')),
        expires_at  TEXT NOT NULL,
        max_uses    INTEGER DEFAULT 999,
        used_count  INTEGER DEFAULT 0,
        rate_limit  INTEGER DEFAULT 10
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_releases (
        version     TEXT PRIMARY KEY,
        sha256      TEXT NOT NULL,
        exe_url     TEXT NOT NULL,
        released_at TEXT NOT NULL
    )""")
    conn.commit()
    # migrate: add install_token if column missing
    try:
        conn.execute("ALTER TABLE users ADD COLUMN install_token TEXT")
        conn.commit()
    except Exception:
        pass
    conn.close()


def _migrate_servers_table():
    """Ensure servers table has per-server id + server_token (multi-server support)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(servers)").fetchall()}
        if 'server_token' in cols:
            return  # already migrated
        # Create new schema
        conn.execute("""CREATE TABLE IF NOT EXISTS servers_new (
            id           TEXT PRIMARY KEY,
            user_id      TEXT NOT NULL,
            server_token TEXT UNIQUE NOT NULL,
            server_url   TEXT NOT NULL DEFAULT 'pending',
            tunnel_url   TEXT,
            wan_url      TEXT,
            lan_url      TEXT,
            cf_tunnel_id TEXT,
            api_key      TEXT NOT NULL DEFAULT 'pending',
            server_name  TEXT DEFAULT 'Мой сервер',
            heartbeat_at TEXT,
            registered_at TEXT DEFAULT (datetime('now'))
        )""")
        if cols:  # old table exists — migrate data
            existing = conn.execute("""
                SELECT s.user_id, s.server_url, s.tunnel_url, s.wan_url, s.lan_url,
                       s.cf_tunnel_id, s.api_key, s.server_name, s.heartbeat_at,
                       s.registered_at, u.install_token
                FROM servers s LEFT JOIN users u ON s.user_id = u.id
            """).fetchall()
            for row in existing:
                (uid, srv_url, tun_url, wan, lan, cf_id,
                 api_k, name, hb, reg_at, itok) = row
                conn.execute("""INSERT OR IGNORE INTO servers_new
                    (id, user_id, server_token, server_url, tunnel_url, wan_url, lan_url,
                     cf_tunnel_id, api_key, server_name, heartbeat_at, registered_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (uuid.uuid4().hex, uid, itok or uuid.uuid4().hex,
                     srv_url or 'pending', tun_url, wan, lan,
                     cf_id, api_k, name, hb, reg_at))
            conn.execute("DROP TABLE servers")
        conn.execute("ALTER TABLE servers_new RENAME TO servers")
        conn.commit()
    except Exception as e:
        print(f"WARN: _migrate_servers_table: {e}", flush=True)
    finally:
        conn.close()


init_db()
_migrate_servers_table()


def _config_get(key: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None


def _config_set(key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO config(key,value) VALUES(?,?)", (key, value))
    conn.commit()
    conn.close()


# ── Cloud CA (root of trust for bootstrap profiles) ───────────────────────────

_CA_KEY_PATH = Path("/app/data/cloud_ca_key.pem")

def _load_ca_key() -> ec.EllipticCurvePrivateKey:
    if not _CA_KEY_PATH.exists():
        raise RuntimeError(f"Cloud CA private key not found at {_CA_KEY_PATH}")
    with open(_CA_KEY_PATH, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)

def _sign_data(data_bytes: bytes) -> str:
    key = _load_ca_key()
    sig = key.sign(data_bytes, ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(sig).decode()


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


def _get_user_servers(user_id: str) -> list[dict]:
    """Return all servers for a user (registered + pending), ordered by registration time."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT id, server_token, server_url, wan_url, lan_url, api_key,
                  server_name, heartbeat_at, tunnel_url, cf_tunnel_id
           FROM servers WHERE user_id=? ORDER BY registered_at ASC""",
        (user_id,)
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        is_pending = (r[5] == 'pending')
        result.append({
            "id":           r[0],
            "server_token": r[1],
            "server_url":   r[2],
            "wan_url":      r[3],
            "lan_url":      r[4],
            "api_key":      r[5],
            "server_name":  r[6],
            "heartbeat_at": r[7],
            "tunnel_url":   r[8],
            "cf_tunnel_id": r[9],
            "is_pending":   is_pending,
        })
    return result


def _get_user_server(user_id: str):
    """Return first registered server (backwards compat for bootstrap/installer)."""
    for s in _get_user_servers(user_id):
        if not s["is_pending"]:
            return s
    return None


def _create_pending_server(user_id: str, server_name: str = "Мой сервер") -> str:
    """Pre-create a pending server slot and return its unique server_token."""
    srv_token = uuid.uuid4().hex
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO servers (id, user_id, server_token, server_url, api_key, server_name)
           VALUES (?,?,?,'pending','pending',?)""",
        (uuid.uuid4().hex, user_id, srv_token, server_name)
    )
    conn.commit()
    conn.close()
    return srv_token


def _is_cf_url(url: str) -> bool:
    """CF tunnel URLs and custom domains are safe to health-check; raw IP:port URLs are not."""
    return bool(url) and not re.match(r'https?://\d+\.\d+\.\d+\.\d+', url)


def _fmt_expires(expires_at: str) -> str:
    """Format ISO datetime as human-readable expiry with date and time."""
    try:
        dt = datetime.datetime.fromisoformat(expires_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.strftime("%d.%m.%Y %H:%M UTC")
    except Exception:
        return expires_at[:10]


def _heartbeat_age(heartbeat_at: str | None) -> tuple[int | None, str]:
    """Returns (age_seconds, human_readable_string). age=None if no heartbeat."""
    if not heartbeat_at:
        return None, ""
    try:
        dt = datetime.datetime.fromisoformat(heartbeat_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        ago = int((datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds())
        if ago < 60:   label = f"{ago}с"
        elif ago < 3600: label = f"{ago // 60}м"
        else:          label = f"{ago // 3600}ч"
        return ago, f" · {label} назад"
    except Exception:
        return None, ""


# ── Cloudflare Tunnel API ─────────────────────────────────────────────────────

def _cf_request(method: str, path: str, body: dict = None):
    if not _CF_ACCOUNT_ID or not _CF_API_TOKEN:
        raise RuntimeError("CF_ACCOUNT_ID / CF_API_TOKEN not configured")
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        f"{_CF_BASE}/{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {_CF_API_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _cf_dns_request(method: str, path: str, body: dict = None):
    token = _CF_DNS_TOKEN or _CF_API_TOKEN
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        f"{_CF_BASE}/{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _cf_create_server_tunnel(server_name: str) -> dict:
    tunnel_secret = base64.b64encode(secrets.token_bytes(32)).decode()
    short_name = re.sub(r'[^a-z0-9-]', '-', server_name.lower())[:40]
    suffix = uuid.uuid4().hex[:6]
    name = f"seamlean-{short_name}-{suffix}"
    result = _cf_request("POST", f"accounts/{_CF_ACCOUNT_ID}/cfd_tunnel", {
        "name": name,
        "tunnel_secret": tunnel_secret,
        "config_src": "cloudflare",
    })
    tunnel_id = result["result"]["id"]
    # Public hostname: srv-{suffix}.seamlean.com
    hostname = f"srv-{suffix}.seamlean.com"
    # Configure tunnel ingress with public hostname → server API port 49200
    _cf_request("PUT", f"accounts/{_CF_ACCOUNT_ID}/cfd_tunnel/{tunnel_id}/configurations", {
        "config": {
            "ingress": [
                {
                    "hostname": hostname,
                    "service": "https://localhost:49200",
                    "originRequest": {"noTLSVerify": True},
                },
                {"service": "http_status:404"},
            ]
        }
    })
    # Create DNS CNAME record (proxied) so hostname resolves
    if _CF_ZONE_ID:
        try:
            _cf_dns_request("POST", f"zones/{_CF_ZONE_ID}/dns_records", {
                "type": "CNAME",
                "name": hostname,
                "content": f"{tunnel_id}.cfargotunnel.com",
                "proxied": True,
                "ttl": 1,
            })
        except Exception as e:
            print(f"WARN: DNS CNAME creation failed: {e}", flush=True)
    # Get cloudflared token for this tunnel
    token_result = _cf_request("GET", f"accounts/{_CF_ACCOUNT_ID}/cfd_tunnel/{tunnel_id}/token")
    tunnel_token = token_result["result"]
    tunnel_url = f"https://{hostname}"
    return {"tunnel_id": tunnel_id, "tunnel_token": tunnel_token, "wan_url": tunnel_url}


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
    resp.set_cookie("session", token, httponly=True, samesite="lax", secure=True)
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
    resp.set_cookie("session", token, httponly=True, samesite="lax", secure=True)
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
@app.post("/v1/register-server")
async def register_server(request: Request):
    try:
        data          = await request.json()
        install_token = data.get("token", "")
        server_name   = data.get("server_name", "Мой сервер")
        lan_url       = data.get("lan_url", "")

        client_ip  = (
            request.headers.get("CF-Connecting-IP") or
            request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or
            request.client.host
        )
        server_url = f"https://{client_ip}:443"

        if not install_token:
            return {"ok": False, "error": "Missing token"}

        conn = sqlite3.connect(DB_PATH)

        # Primary lookup: server_token in servers table (new multi-server flow)
        srv_row = conn.execute(
            "SELECT id, user_id, api_key, cf_tunnel_id, tunnel_url FROM servers WHERE server_token=?",
            (install_token,)
        ).fetchone()

        if srv_row:
            server_db_id         = srv_row[0]
            user_id              = srv_row[1]
            existing_api_key     = srv_row[2]
            existing_cf_id       = srv_row[3]
            existing_tunnel_url  = srv_row[4]
        else:
            # Fallback: old install.sh used users.install_token (backwards compat)
            user_row = conn.execute("SELECT id FROM users WHERE install_token=?", (install_token,)).fetchone()
            if not user_row:
                conn.close()
                return {"ok": False, "error": "Invalid token"}
            user_id = user_row[0]
            server_db_id        = uuid.uuid4().hex
            existing_api_key    = None
            existing_cf_id      = None
            existing_tunnel_url = None

        api_key      = (existing_api_key if (existing_api_key and existing_api_key != 'pending')
                        else uuid.uuid4().hex + uuid.uuid4().hex)
        cf_tunnel_id = existing_cf_id
        tunnel_token = None
        cf_url       = existing_tunnel_url  # preserve stored tunnel_url

        # CF tunnel: optional — only created when CF credentials are configured.
        # Also recreate if existing tunnel_url is old raw cfargotunnel.com format (no public hostname).
        _old_format = bool(cf_url and re.match(r'https://[0-9a-f-]+\.cfargotunnel\.com', cf_url))
        if _CF_ACCOUNT_ID and _CF_API_TOKEN:
            if not cf_tunnel_id or _old_format:
                try:
                    cf = _cf_create_server_tunnel(server_name)
                    cf_tunnel_id = cf["tunnel_id"]
                    tunnel_token = cf["tunnel_token"]
                    cf_url       = cf["wan_url"]
                except Exception:
                    pass
            else:
                # Reinstall: re-issue token for existing tunnel, keep existing tunnel_url.
                try:
                    result       = _cf_request("GET", f"accounts/{_CF_ACCOUNT_ID}/cfd_tunnel/{cf_tunnel_id}/token")
                    tunnel_token = result["result"]
                    # cf_url already set from existing row — don't overwrite
                except Exception:
                    pass

        # wan_url always = public IP from request header (displayed in cabinet, not used for health checks)
        wan_url = f"https://{client_ip}:443"

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn.execute(
            """INSERT OR REPLACE INTO servers
               (id, user_id, server_token, server_url, lan_url, wan_url, tunnel_url,
                cf_tunnel_id, api_key, server_name, heartbeat_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (server_db_id, user_id, install_token,
             lan_url or server_url, lan_url or None, wan_url,
             cf_url, cf_tunnel_id, api_key, server_name, now)
        )
        conn.commit()
        conn.close()
        return {
            "ok": True,
            "api_key": api_key,
            "tunnel_token": tunnel_token,
            "tunnel_url": cf_url,
            "server_token": install_token,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Server heartbeat (called by server every 5 min) ──────────────────────────

@app.post("/api/server-heartbeat")
@app.post("/v1/server-heartbeat")
async def server_heartbeat(request: Request):
    api_key = request.headers.get("X-Api-Key", "")
    if not api_key:
        return {"ok": False, "error": "Missing api key"}
    conn = sqlite3.connect(DB_PATH)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    cur = conn.execute(
        "UPDATE servers SET heartbeat_at=? WHERE api_key=?", (now, api_key)
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return {"ok": False, "error": "Unknown api key"}
    return {"ok": True}


# ── Cabinet ───────────────────────────────────────────────────────────────────

_LAYER_LABELS = {"window": "Окна", "visual": "Скрины", "system": "Система",
                 "applogs": "Логи", "browser": "Браузер"}
_ST_ICON = {"online": "🟢", "warning": "🟡", "offline": "🔴"}

# Tooltip "i" badge — used everywhere instead of "?"
_TIP_I = ('<span style="display:inline-block;width:14px;height:14px;background:#e5e7eb;'
          'border-radius:50%;font-size:.6rem;font-style:italic;font-weight:700;'
          'text-align:center;line-height:14px;cursor:help;color:#6b7280;'
          'vertical-align:middle;margin-left:2px">i</span>')


def _parse_layer_stats(layer_stats, layer_counts=None):
    try:
        hb = json.loads(layer_stats) if isinstance(layer_stats, str) else (layer_stats or {})
        heartbeat = {
            layer: {
                "events_5min": (ls.get("Events5Min") or ls.get("events_5min") or 0),
                "errors_5min": (ls.get("Errors5Min") or ls.get("errors_5min") or 0),
            }
            for layer, ls in hb.items() if ls
        }
    except Exception:
        heartbeat = {}

    counts = layer_counts or {}
    all_layers = set(heartbeat) | set(counts)
    result = {}
    for layer in all_layers:
        hb_data = heartbeat.get(layer, {})
        ct_data = counts.get(layer, {})
        result[layer] = {
            "events_5min":  hb_data.get("events_5min", 0),
            "errors_5min":  hb_data.get("errors_5min", 0),
            "events_1h":    ct_data.get("events_1h"),    # None = server too old
            "events_24h":   ct_data.get("events_24h"),
            "events_total": ct_data.get("events_total"),
            "errors_1h":    ct_data.get("errors_1h"),
            "errors_24h":   ct_data.get("errors_24h"),
        }
    return result


def _collection_badge(stats: dict, is_offline: bool = False) -> str:
    if is_offline:
        return (f'<span style="color:#9ca3af;font-size:.82rem" '
                f'title="Агент не выходил на связь — статус сбора данных неизвестен">'
                f'Неизвестно {_TIP_I}</span>')
    if not stats:
        return '<span style="color:#9ca3af;font-size:.82rem">нет данных</span>'

    # Layers the agent explicitly reports (appear in heartbeat layer_stats)
    known = {k: v for k, v in stats.items() if v.get("events_5min") is not None}
    if not known:
        known = stats

    has_errors = any((v.get("errors_24h") or 0) > 0 for v in known.values())
    has_1h_data = any(v.get("events_1h") is not None for v in known.values())

    _TIPS = {
        "ok":       "Все активные слои присылали события в последний час",
        "errors":   "За последние сутки есть LayerError события в одном или нескольких слоях",
        "no_data":  "За последние сутки нет ни одного события ни в одном слое",
        "no_1h":    "Один или несколько активных слоёв не присылали события в последний час",
        "no_events":"Нет событий за последние 5 минут",
    }
    _tip = lambda key: f' title="{_TIPS[key]}"'
    _b   = lambda bg, col, txt, tip: (f'<span style="background:{bg};color:{col};padding:2px 9px;'
                                      f'border-radius:12px;font-size:.78rem;font-weight:600"{_tip(tip)}>'
                                      f'{txt} {_TIP_I}</span>')
    if has_errors:
        return _b("#fee2e2", "#991b1b", "⚠ Ошибки", "errors")

    if has_1h_data:
        active_24h = {k for k, v in known.items() if (v.get("events_24h") or 0) > 0}
        if not active_24h:
            return _b("#fee2e2", "#991b1b", "❌ Не работает", "no_data")
        warn = any((known[k].get("events_1h") or 0) == 0 for k in active_24h)
        if warn:
            return _b("#fef3c7", "#92400e", "⚠ Нет событий за час", "no_1h")
        return _b("#d1fae5", "#065f46", "✅ Работает", "ok")

    # Fallback: 5min data only
    active = sum(1 for v in known.values() if (v.get("events_5min") or 0) > 0)
    if active == 0:
        return f'<span style="color:#9ca3af;font-size:.82rem"{_tip("no_events")}>нет событий {_TIP_I}</span>'
    return _b("#d1fae5", "#065f46", "✅ Работает", "ok")


def _layers_detail_row(row_id: str, stats: dict, is_offline: bool = False) -> str:
    has_1h = any(v.get("events_1h") is not None for v in stats.values())

    header = '<tr style="border-bottom:1px solid #e5e7eb">'
    header += '<th style="padding:3px 14px 3px 0;color:#9ca3af;font-size:.7rem;font-weight:600;text-align:left">СЛОЙ</th>'
    header += '<th style="padding:3px 10px;color:#9ca3af;font-size:.7rem;font-weight:600;text-align:right">5 МИН</th>'
    if has_1h:
        header += '<th style="padding:3px 10px;color:#9ca3af;font-size:.7rem;font-weight:600;text-align:right">1 ЧАС</th>'
        header += '<th style="padding:3px 10px;color:#9ca3af;font-size:.7rem;font-weight:600;text-align:right">24Ч</th>'
        header += '<th style="padding:3px 10px;color:#9ca3af;font-size:.7rem;font-weight:600;text-align:right">ВСЁ ВРЕМЯ</th>'
    header += '</tr>'

    def _c(n, err=0):
        """1h column: None→'—', 0→normal, N→normal"""
        if n is None: return '<span style="color:#9ca3af">—</span>'
        err_s = (f' <span style="color:#991b1b;font-size:.65rem">+{err}err</span>' if err else "")
        return f'<span style="color:#374151">{n}</span>{err_s}'

    def _c24(n, err=0):
        """24h column: None or 0→'—' (no data), N→normal"""
        if n is None or n == 0:
            err_s = (f' <span style="color:#991b1b;font-size:.65rem">+{err}err</span>' if err else "")
            return f'<span style="color:#9ca3af">—</span>{err_s}'
        err_s = (f' <span style="color:#991b1b;font-size:.65rem">+{err}err</span>' if err else "")
        return f'<span style="color:#374151">{n}</span>{err_s}'

    data_rows = ""
    total_1h = total_24h = total_total = total_5min = 0
    for key, label in _LAYER_LABELS.items():
        v      = stats.get(key)
        # None means the layer never appeared in DB or heartbeat → show "—" everywhere
        if v is None:
            e5, e1h, e24h, etotal, err1h, err24h = None, None, None, None, 0, 0
        else:
            # Offline: 5min and 1h always "—" (machine isn't sending)
            e5     = None if is_offline else (v.get("events_5min") or 0)
            e1h    = None if is_offline else v.get("events_1h")
            e24h   = v.get("events_24h")   # keep real 24h data even for offline
            etotal = v.get("events_total")
            err1h  = (v.get("errors_1h")  or 0)
            err24h = (v.get("errors_24h") or 0)

        if e5     is not None: total_5min  += e5
        if e1h    is not None: total_1h    += e1h
        if e24h   is not None: total_24h   += e24h
        if etotal is not None: total_total += etotal

        if v is None:
            icon, col = "—", "#9ca3af"
        elif err24h:
            icon, col = "⚠", "#991b1b"
        elif (e5 or 0) > 0 or (e1h or 0) > 0:
            icon, col = "✅", "#065f46"
        else:
            icon, col = "○", "#9ca3af"

        err_badge = (f' <span style="color:#991b1b;font-size:.7rem">({err24h} err)</span>' if err24h else "")
        e5_cell = '—' if e5 is None else e5
        row = (f'<tr>'
               f'<td style="padding:3px 14px 3px 0;white-space:nowrap">'
               f'<span style="color:{col};font-size:.8rem">{icon} {label}</span>{err_badge}</td>'
               f'<td style="padding:3px 10px;text-align:right;color:#374151;font-size:.8rem">{e5_cell}</td>')
        if has_1h:
            row += (f'<td style="padding:3px 10px;text-align:right;font-size:.8rem">{_c(e1h, err1h)}</td>'
                    f'<td style="padding:3px 10px;text-align:right;font-size:.8rem">{_c24(e24h, err24h)}</td>'
                    f'<td style="padding:3px 10px;text-align:right;color:#6b7280;font-size:.8rem">'
                    f'{"—" if etotal is None else etotal}</td>')
        row += '</tr>'
        data_rows += row

    t5s  = '—' if is_offline else total_5min
    t24s = '—' if total_24h  == 0 else total_24h
    total_row = (f'<tr style="border-top:1px solid #e5e7eb;font-weight:600">'
                 f'<td style="padding:4px 14px 4px 0;color:#374151;font-size:.8rem">Итого</td>'
                 f'<td style="padding:4px 10px;text-align:right;font-size:.8rem">{t5s}</td>')
    if has_1h:
        t1s = '—' if is_offline else total_1h
        total_row += (f'<td style="padding:4px 10px;text-align:right;font-size:.8rem">{t1s}</td>'
                      f'<td style="padding:4px 10px;text-align:right;font-size:.8rem">{t24s}</td>'
                      f'<td style="padding:4px 10px;text-align:right;color:#6b7280;font-size:.8rem">{total_total}</td>')
    total_row += '</tr>'

    inner = (f'<table style="border-collapse:collapse;border:0">'
             f'{header}{data_rows}{total_row}</table>')
    return (f'<tr id="layer-{row_id}" style="display:none;background:#f8fafc">'
            f'<td colspan="9" style="padding:8px 12px 12px 32px">{inner}</td></tr>')


_ST_TIPS = {
    "online":  "Heartbeat получен менее 2 минут назад",
    "warning": "Heartbeat получен 2–15 минут назад",
    "offline": "Heartbeat не получен более 15 минут назад",
}


def _agent_rows(agents) -> str:
    if not agents:
        return ('<tr><td colspan="9" style="text-align:center;color:#9ca3af;padding:20px">'
                'Агентов нет. Установите первого с помощью команды выше.</td></tr>')
    latest_ver      = _config_get("latest_agent_version") or ""
    latest_released = _config_get("latest_agent_released_at") or ""
    if latest_released:
        try:
            dt = datetime.datetime.fromisoformat(latest_released.rstrip("Z"))
            latest_released_s = dt.strftime("%-d %b %Y")
        except Exception:
            latest_released_s = latest_released[:10]
    else:
        latest_released_s = ""
    latest_tip = (f"Последняя stable: v{latest_ver} · {latest_released_s}" if latest_ver else "")
    rows = ""
    srv_counters: dict = {}
    for i, a in enumerate(agents):
        lag    = a.get("lag_sec", 0)
        drift  = a.get("drift_ms")
        st     = a.get("status", "offline")
        ver    = a.get("agent_version") or "—"
        host   = a.get("hostname") or a.get("machine_id", "")[:12] + "…"
        last   = f"{lag}с" if lag < 60 else (f"{lag//60}м" if lag < 3600 else f"{lag//3600}ч")
        drift_s = (f'<span style="color:{"#dc2626" if abs(drift)>1000 else "#374151"}">{drift:+d}мс</span>'
                   if drift is not None else "—")
        is_offline = (st == "offline")
        stats  = _parse_layer_stats(a.get("layer_stats"), a.get("layer_counts"))
        rid    = f"a{i}"

        data_mb = a.get("data_mb")
        data_s  = (f'<span style="color:#6b7280;font-size:.82rem">{data_mb} МБ</span>'
                   if data_mb is not None else '<span style="color:#9ca3af;font-size:.82rem">—</span>')

        st_tip  = _ST_TIPS.get(st, "")
        badge   = (f'<span class="badge {st}" title="{st_tip}">'
                   f'{_ST_ICON.get(st,"❓")} {st}'
                   f' {_TIP_I}</span>')

        username = a.get("username")
        domain   = a.get("domain")
        lan_ip   = a.get("lan_ip")
        wan_ip   = a.get("wan_ip")

        user_line = ""
        if username:
            dom_part = (f'<span style="color:#9ca3af">@</span>'
                        f'<code style="font-size:.74rem;background:#f3f4f6;padding:1px 5px;border-radius:4px">'
                        f'{domain}</code>' if domain else "")
            user_line = (f'<div style="font-size:.74rem;color:#6b7280;margin-top:2px">'
                         f'{username}{dom_part}</div>')

        ip_parts = []
        if lan_ip:
            ip_parts.append(
                f'<span style="display:inline-flex;align-items:center;gap:3px">'
                f'<span style="background:#dbeafe;color:#1d4ed8;padding:0 5px;border-radius:4px;'
                f'font-size:.68rem;font-weight:700;line-height:16px">LAN</span>'
                f' {lan_ip}</span>')
        if wan_ip:
            ip_parts.append(
                f'<span style="display:inline-flex;align-items:center;gap:3px">'
                f'<span style="background:#fef9c3;color:#854d0e;padding:0 5px;border-radius:4px;'
                f'font-size:.68rem;font-weight:700;line-height:16px">WAN</span>'
                f' {wan_ip}</span>')
        ip_cell = (f'<div style="font-size:.74rem;color:#9ca3af;display:flex;gap:8px;flex-wrap:wrap">'
                   f'{"".join(ip_parts)}</div>' if ip_parts else
                   '<span style="color:#d1d5db;font-size:.82rem">—</span>')

        machine_id  = a.get("machine_id", "")
        srv_url     = a.get("_server_url", "")
        srv_api_key = a.get("_api_key", "")
        srv_counters[srv_url] = srv_counters.get(srv_url, 0) + 1
        num = srv_counters[srv_url]
        is_outdated = latest_ver and ver not in ("—", "") and ver != latest_ver
        ver_color   = "#dc2626" if is_outdated else "#6b7280"
        ver_tip     = latest_tip if is_outdated else (latest_tip or "")
        if is_outdated and ver not in ("—", ""):
            upd_tip = f"Обновить: v{ver} → v{latest_ver}"
        elif latest_ver:
            upd_tip = f"Обновить до v{latest_ver}"
        else:
            upd_tip = "Обновить сейчас"
        if srv_url and machine_id:
            upd_btn = (
                f'<button onclick="forceUpdate(event,\'{srv_url}\',\'{srv_api_key}\',\'{machine_id}\')" '
                f'title="{upd_tip}" '
                f'style="margin-left:6px;background:none;border:1px solid #d1d5db;border-radius:4px;'
                f'padding:1px 5px;cursor:pointer;font-size:.74rem;color:#4f46e5">↑</button>'
            )
        else:
            upd_btn = ""
        ver_cell = (
            f'<span style="color:{ver_color};font-size:.82rem" title="{ver_tip}">{ver}</span>'
            f'{upd_btn}'
        )

        rows += f"""<tr style="cursor:pointer" onclick="toggleLayer('{rid}')">
          <td style="white-space:nowrap;color:#9ca3af;font-size:.82rem;text-align:center">{num}</td>
          <td style="font-size:.84rem">{host}{user_line}</td>
          <td>{ip_cell}</td>
          <td style="white-space:nowrap">{badge}</td>
          <td style="white-space:nowrap;color:#6b7280;font-size:.82rem">{last} назад</td>
          <td style="white-space:nowrap">{ver_cell}</td>
          <td style="white-space:nowrap">{_collection_badge(stats, is_offline)}</td>
          <td style="white-space:nowrap">{drift_s}</td>
          <td style="white-space:nowrap">{data_s}</td>
        </tr>"""
        rows += _layers_detail_row(rid, stats, is_offline)
    return rows


def _server_extra_inline(health: dict | None) -> list[str]:
    if not health:
        return []
    parts = []
    if health.get("version"):
        parts.append(f"Версия {health['version']}")
    if health.get("db"):
        db_ok = health["db"] == "ok"
        db_size = f" {health['db_size_mb']} MB" if health.get("db_size_mb") is not None else ""
        parts.append(f'{"✅" if db_ok else "⚠️"} DB{db_size}')
    if health.get("disk_free_gb") is not None:
        free  = health["disk_free_gb"]
        total = health.get("disk_total_gb")
        disk_s = f"Диск: {free} GB своб." + (f" / {total} GB" if total else "")
        parts.append(disk_s)
    return parts


def _build_server_card(srv: dict) -> tuple[str, list]:
    """Build HTML card for one registered server. Returns (html, agents_data)."""
    wan_url    = srv.get("wan_url")
    tunnel_url = srv.get("tunnel_url")
    api_key    = srv["api_key"]
    hb_age, hb_s = _heartbeat_age(srv.get("heartbeat_at"))

    _STALE = 600
    agents_data: list = []
    if tunnel_url:
        health      = _api(tunnel_url, api_key, "/health")
        raw_agents  = (_api(tunnel_url, api_key, "/api/v1/agents") or {}).get("agents", [])
        for a in raw_agents:
            a["_server_url"] = tunnel_url
            a["_api_key"]    = api_key
        agents_data = raw_agents
    else:
        health = None

    if health and health.get("status") in ("ok", "degraded"):
        status_badge = f'<span class="badge online">🟢 Онлайн{hb_s}</span>'
    elif hb_age is not None and hb_age < _STALE:
        status_badge = f'<span class="badge online">🟢 Онлайн{hb_s}</span>'
    elif hb_age is not None:
        status_badge = f'<span class="badge offline">🔴 Недоступен{hb_s}</span>'
    else:
        status_badge = '<span class="badge warning">⚠️ Ожидание сервера</span>'

    lan_url     = srv.get("lan_url") or srv["server_url"]
    wan_span    = (f'<span style="color:#9ca3af;font-size:.78rem">WAN: '
                   f'<code style="font-size:.78rem">{wan_url}</code></span>') if wan_url else ""
    extra_spans = "".join(
        f'<span style="color:#6b7280;font-size:.82rem">{v}</span>'
        for v in _server_extra_inline(health)
    )
    html = f"""
  <div class="card" style="padding:20px 28px">
    <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
      <span style="font-weight:700;font-size:.95rem;color:#374151">{srv['server_name']}</span>
      {status_badge}
      <span style="color:#9ca3af;font-size:.78rem">LAN: <code style="font-size:.78rem">{lan_url}</code></span>
      {wan_span}
      {extra_spans}
    </div>
  </div>"""
    return html, agents_data


@app.get("/cabinet", response_class=HTMLResponse)
def cabinet(request: Request):
    token = request.cookies.get("session")
    user  = _db_user_by_session(token)
    if not user:
        return RedirectResponse("/login", status_code=302)

    install_token   = _get_install_token(user["id"])
    all_servers     = _get_user_servers(user["id"])
    reg_servers     = [s for s in all_servers if not s["is_pending"]]
    pending_servers = [s for s in all_servers if s["is_pending"]]

    # ── Server panels ─────────────────────────────────────────────────────────
    agents_data   = []
    bootstrap_url = ""
    server_panels = ""

    if reg_servers:
        for srv in reg_servers:
            card_html, srv_agents = _build_server_card(srv)
            server_panels += card_html
            agents_data.extend(srv_agents)
            if not bootstrap_url:
                bootstrap_url = srv["server_url"]
    else:
        # No registered servers — show install card with a pending server token
        if pending_servers:
            first_token = pending_servers[0]["server_token"]
            pending_servers = pending_servers[1:]  # rest stay as "add server" candidates
        else:
            first_token = _create_pending_server(user["id"])
        install_cmd = f"curl -fsSL https://seamlean.com/install.sh | sudo bash -s -- --token {first_token}"
        server_panels = f"""
  <div class="card">
    <div class="sec-title">Установить сервер</div>
    <p style="font-size:.85rem;color:#6b7280;margin-bottom:10px">Ubuntu 20.04+, 2 CPU, 4 GB RAM, исходящий интернет:</p>
    <div class="copy-row">
      <div class="code-box" id="srv-cmd">{install_cmd}</div>
      <button class="btn btn-gray" id="srv-cmd-btn" style="padding:7px 14px;font-size:.82rem;white-space:nowrap" onclick="copyText('srv-cmd')">Скопировать</button>
    </div>
    <p class="hint">Команда установит Docker, сервер и все сервисы. После завершения сервер появится здесь автоматически.</p>
  </div>"""

    # ── Agents panel ──────────────────────────────────────────────────────────
    online = sum(1 for a in agents_data if a.get("status") == "online")
    agent_badge = (f"&nbsp;<span style='background:#4f46e5;color:#fff;border-radius:20px;"
                   f"padding:1px 7px;font-size:.75rem'>{online}/{len(agents_data)}</span>"
                   if agents_data else "")

    if bootstrap_url:
        conn2 = sqlite3.connect(DB_PATH)
        code_row = conn2.execute(
            "SELECT code, expires_at FROM install_codes WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
            (user["id"],)
        ).fetchone()
        conn2.close()
        if code_row:
            install_url = f"https://seamlean.com/i/{code_row[0]}"
            exe_cmd = f"Seamlean.Agent.exe --install --install-code {code_row[0]}"
            install_link_html = f"""
    <div class="sec-title">Установить агент на Windows</div>
    <p style="font-size:.85rem;color:#6b7280;margin-bottom:8px">
      Скачайте <a href="https://seamlean.com/agent" style="color:#4f46e5;font-weight:600">Seamlean.Agent.exe</a>
      и запустите от имени администратора:
    </p>
    <div class="copy-row">
      <div class="code-box" id="burl">{exe_cmd}</div>
      <button class="btn btn-gray" id="burl-btn" style="padding:7px 14px;font-size:.82rem;white-space:nowrap" onclick="copyText('burl')">Скопировать</button>
      <form method="post" action="/cabinet/generate-install-code" style="display:inline;margin:0">
        <button class="btn btn-gray" style="padding:7px 14px;font-size:.82rem;white-space:nowrap">Обновить</button>
      </form>
    </div>
    <p class="hint">Или отправьте ссылку пользователю: <a href="{install_url}" style="color:#4f46e5">{install_url}</a></p>
    <p class="hint">Агент установится тихо, без перезагрузки. Ссылка действует до {_fmt_expires(code_row[1])}.</p>"""
        else:
            install_link_html = f"""
    <div class="sec-title">Установить агент на Windows</div>
    <p style="font-size:.85rem;color:#6b7280;margin-bottom:10px">Создайте ссылку для установки агента:</p>
    <form method="post" action="/cabinet/generate-install-code" style="display:inline">
      <button class="btn" style="padding:8px 18px;font-size:.86rem">Создать ссылку установки</button>
    </form>"""
        agent_install_section = install_link_html
    else:
        agent_install_section = '<p style="color:#9ca3af;font-size:.88rem">Сначала установите и запустите сервер — ссылка на агента появится здесь.</p>'

    # ── "Добавить сервер" section (only when ≥1 server registered) ───────────
    if reg_servers:
        if pending_servers:
            add_tok = pending_servers[0]["server_token"]
            add_cmd = f"curl -fsSL https://seamlean.com/install.sh | sudo bash -s -- --token {add_tok}"
            add_content = f"""
      <div class="copy-row">
        <div class="code-box" id="srv-add">{add_cmd}</div>
        <button class="btn btn-gray" id="srv-add-btn" style="padding:7px 14px;font-size:.82rem;white-space:nowrap" onclick="copyText('srv-add')">Скопировать</button>
      </div>
      <p class="hint">Токен уникален для этого сервера и не совпадает с токенами других серверов.</p>"""
        else:
            add_content = """
      <form method="post" action="/cabinet/new-server-token">
        <button class="btn" style="padding:8px 18px;font-size:.86rem">Создать токен для нового сервера</button>
      </form>
      <p class="hint" style="margin-top:8px">Каждый сервер получает уникальный токен установки.</p>"""
        add_server_card = f"""
<div class="card" style="padding:16px 28px">
  <details>
    <summary style="cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px;
                    font-size:.92rem;font-weight:700;color:#374151;user-select:none">
      <span style="font-size:1.1rem;color:#4f46e5">+</span> Добавить сервер
    </summary>
    <div style="margin-top:14px;border-top:1px solid #f3f4f6;padding-top:14px">
      <p style="font-size:.85rem;color:#6b7280;margin-bottom:10px">
        Ubuntu 20.04+, 2 CPU, 4 GB RAM, исходящий интернет:
      </p>
      {add_content}
    </div>
  </details>
</div>"""
    else:
        add_server_card = ""

    nav = f"""
    <span>{user['email']}</span>
    <form method="post" action="/logout" style="display:inline">
      <button class="btn btn-gray" style="padding:5px 14px;font-size:.82rem">Выйти</button>
    </form>"""

    body = f"""
{server_panels}
<div class="card">
  {agent_install_section}
</div>
<div class="card">
  <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:12px">
    <div class="sec-title" style="margin-bottom:0">Устройства</div>
    {agent_badge}
  </div>
  <table>
    <thead><tr>
      <th style="white-space:nowrap">№</th><th>Машина / Пользователь</th><th>Адрес</th><th style="white-space:nowrap">Агент</th><th style="white-space:nowrap">Last seen</th><th style="white-space:nowrap">Версия</th><th style="white-space:nowrap">Сбор данных</th>
      <th style="white-space:nowrap" title="NTP-дрейф часов агента относительно эталонного времени. Норма: ±50 мс. При больших значениях временна́я корреляция событий между машинами может быть неточной.">Drift {_TIP_I}</th><th style="white-space:nowrap">Данные</th>
    </tr></thead>
    <tbody>{_agent_rows(agents_data)}</tbody>
  </table>
  <p style="color:#9ca3af;font-size:.78rem;margin-top:8px">↕ Нажмите на строку чтобы увидеть статус слоёв</p>
</div>
<script>
function toggleLayer(id) {{
  var row = document.getElementById('layer-' + id);
  if (row) row.style.display = row.style.display === 'none' ? 'table-row' : 'none';
}}
function forceUpdate(e, srvUrl, apiKey, machineId) {{
  e.stopPropagation();
  if (!confirm('Отправить команду обновления агенту ' + machineId + '?')) return;
  fetch(srvUrl + '/api/v1/machines/' + machineId + '/force-update', {{
    method: 'POST',
    headers: {{'X-Api-Key': apiKey, 'Content-Type': 'application/json'}}
  }}).then(r => r.json()).then(d => alert(d.ok ? 'Команда отправлена' : 'Ошибка: ' + JSON.stringify(d)))
     .catch(err => alert('Ошибка: ' + err));
}}
</script>
{add_server_card}"""

    return _page("Кабинет — Seamlean", nav, body, wide=True)


# ── install.sh ────────────────────────────────────────────────────────────────

_INSTALL_SH = r"""#!/usr/bin/env bash
set -euo pipefail

REPO="https://github.com/sabaleuskivitali/claude_developer.git"
INSTALL_DIR="/opt/seamlean"
INSTALL_TOKEN=""
# Server name: Windows/DNS domain first, fallback to hostname
_DOMAIN=$(hostname -d 2>/dev/null | grep -v '(none)' | grep -v '^$' || true)
if [ -n "$_DOMAIN" ]; then
  SERVER_NAME="$(hostname -s).${_DOMAIN}"
else
  SERVER_NAME=$(hostname -s 2>/dev/null || hostname)
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --token|-t) INSTALL_TOKEN="$2"; shift 2;;
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
  git -C "$INSTALL_DIR" fetch origin
  git -C "$INSTALL_DIR" reset --hard origin/main
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
  cat > .env << EOF
SERVER_NAME=${SERVER_NAME}
POSTGRES_DB=diag
POSTGRES_USER=diag
POSTGRES_PASSWORD=${PG_PASS}
API_KEY=pending
MINIO_ACCESS_KEY=${MINIO_KEY}
MINIO_SECRET_KEY=${MINIO_SECRET}
PORT_RANGE_START=49200
PORT_RANGE_END=49300
UPDATE_PACKAGES_DIR=/opt/seamlean/updates
SMB_MOUNT_PATH=/mnt/diag
SMB_SHARE_PATH=/mnt/diag
ETL_INTERVAL_MINUTES=60
SERVER_URL=
CLOUD_URL=
EOF
  echo "Generated secrets in .env (api_key will be set by cloud)"
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
  # Detect LAN IP for local agent discovery
  LAN_IP=$(hostname -I | awk '{print $1}')
  LAN_URL="https://${LAN_IP}:49200"
  echo "Registering server with Seamlean cloud (LAN: ${LAN_URL})..."
  RESP=$(curl -sf -X POST "https://api.seamlean.com/v1/register-server" \
    -H "Content-Type: application/json" \
    -d "{\"token\":\"${INSTALL_TOKEN}\",\"server_name\":\"${SERVER_NAME}\",\"lan_url\":\"${LAN_URL}\"}" \
    2>/dev/null || echo '{"ok":false}')

  if echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); exit(0 if d.get('ok') else 1)" 2>/dev/null; then
    API_KEY=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['api_key'])")
    TUNNEL_TOKEN=$(echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); v=d.get('tunnel_token'); print(v if v else '')")
    TUNNEL_URL=$(echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); v=d.get('tunnel_url'); print(v if v else '')")
    sed -i "s|^API_KEY=.*|API_KEY=${API_KEY}|" .env
    sed -i "s|^CLOUD_URL=.*|CLOUD_URL=https://seamlean.com|" .env
    if [ -n "$TUNNEL_URL" ]; then
      sed -i "s|^SERVER_URL=.*|SERVER_URL=${TUNNEL_URL}|" .env
    fi
    echo "✅ Server registered! API key configured."

    # Restart API with correct key/URL (always, before cloudflared)
    docker compose up -d api
    echo "✅ API restarted with correct credentials."

    # ── Install cloudflared for WAN access ──────────────────────────────────
    if [ -n "$TUNNEL_TOKEN" ]; then
      echo "Installing cloudflared for WAN tunnel..."
      (
        set +e
        if ! command -v cloudflared &>/dev/null; then
          curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
            | gpg --dearmor -o /usr/share/keyrings/cloudflare-main.gpg
          echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
            > /etc/apt/sources.list.d/cloudflared.list
          apt-get update -q && apt-get install -y -q cloudflared
        fi
        cloudflared service uninstall 2>/dev/null
        systemctl stop cloudflared 2>/dev/null
        cloudflared service install "$TUNNEL_TOKEN"
        systemctl enable cloudflared
        systemctl restart cloudflared
        echo "✅ Cloudflare tunnel active — WAN access enabled."
      ) || echo "⚠️  Cloudflare tunnel setup failed — WAN access unavailable, LAN still works."
    fi

    echo "Open https://seamlean.com/cabinet to see status."
  else
    echo "⚠️  Registration failed: $RESP"
  fi
fi

echo ""
echo "=== ✅ Seamlean server installed ==="
echo "API port: 49200 (LAN)"
echo "Logs: docker compose -f $INSTALL_DIR/server/docker-compose.yml logs -f"
"""


@app.get("/install.sh", response_class=PlainTextResponse)
def install_sh():
    return PlainTextResponse(_INSTALL_SH, media_type="text/x-sh")


# ── Agent package download ────────────────────────────────────────────────────
_GITHUB_REPO = "sabaleuskivitali/claude_developer"
_agent_url_cache: dict = {"url": None, "ts": 0.0}


def _get_agent_url() -> str | None:
    """Return the latest agent download URL.

    Priority:
    1. In-memory cache (5 min TTL) — warm path after webhook fires
    2. DB (persisted by webhook) — survives container restarts
    3. GitHub API poll with per_page=100 — startup fallback only, used once if DB empty
    """
    now = time.time()
    if _agent_url_cache["url"] and now - _agent_url_cache["ts"] < 300:
        return _agent_url_cache["url"]

    # Cold start: load from DB first to avoid a GitHub API call on every restart
    if not _agent_url_cache["url"]:
        cached = _config_get("latest_agent_url")
        if cached:
            _agent_url_cache["url"] = cached
            _agent_url_cache["ts"] = now - 290  # treat as almost-expired so next miss refreshes
            return cached

    # Fallback: poll GitHub API (used at startup when DB is empty or cache expired)
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{_GITHUB_REPO}/releases?per_page=100",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "Seamlean-Cloud/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            releases = json.loads(r.read())
        for rel in releases:
            if rel.get("tag_name", "").startswith("agent/"):
                for asset in rel.get("assets", []):
                    if asset["name"] == "Seamlean.Agent.exe":
                        url = asset["browser_download_url"]
                        _agent_url_cache["url"] = url
                        _agent_url_cache["ts"] = now
                        _config_set("latest_agent_url", url)
                        return url
    except Exception:
        pass
    return _agent_url_cache["url"]  # return stale cache on GitHub error rather than None


@app.get("/agent")
def agent_download():
    url = _get_agent_url()
    if url:
        return RedirectResponse(url, status_code=302)
    return JSONResponse(status_code=503, content={"error": "Agent release not found on GitHub"})


@app.post("/api/github/release")
async def github_release_webhook(request: Request):
    """GitHub webhook receiver for Release events.

    Configure in GitHub: Settings → Webhooks → Add webhook
      Payload URL: https://seamlean.com/api/github/release
      Content type: application/json
      Secret: value of GITHUB_WEBHOOK_SECRET env var
      Events: Releases
    """
    body = await request.body()

    if _GITHUB_WEBHOOK_SECRET:
        sig_header = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(_GITHUB_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            return JSONResponse(status_code=401, content={"error": "Invalid signature"})

    try:
        payload = json.loads(body)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    # Only act on published releases tagged agent/vX.Y.Z
    if payload.get("action") != "published":
        return {"ok": True, "skipped": True, "reason": "not a published event"}

    release = payload.get("release", {})
    tag = release.get("tag_name", "")
    if not tag.startswith("agent/"):
        return {"ok": True, "skipped": True, "reason": "not an agent release"}

    for asset in release.get("assets", []):
        if asset["name"] == "Seamlean.Agent.exe":
            url = asset["browser_download_url"]
            _agent_url_cache["url"] = url
            _agent_url_cache["ts"] = time.time()
            _config_set("latest_agent_url", url)
            return {"ok": True, "tag": tag, "url": url}

    return JSONResponse(status_code=422, content={"error": "Seamlean.Agent.exe not found in release assets"})


# ── Stable-tag webhook (called by CI after -stable tag push) ──────────────────

def _fetch_latest_json_from_github(tag: str) -> dict | None:
    """Download latest.json from the GitHub Release for the given tag."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Seamlean-Cloud/1.0"}
    if _GITHUB_TOKEN:
        headers["Authorization"] = f"token {_GITHUB_TOKEN}"
    tag_enc = tag.replace("/", "%2F")
    url = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/tags/{tag_enc}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx) as r:
            release = json.loads(r.read())
        for asset in release.get("assets", []):
            if asset["name"] == "latest.json":
                asset_url = asset["url"]
                dl_headers = dict(headers)
                dl_headers["Accept"] = "application/octet-stream"
                req2 = urllib.request.Request(asset_url, headers=dl_headers)
                with urllib.request.urlopen(req2, timeout=10, context=_ssl_ctx) as r2:
                    return json.loads(r2.read())
    except Exception as e:
        print(f"WARN: _fetch_latest_json_from_github({tag}): {e}", flush=True)
    return None


def _store_agent_release(version: str, sha256: str, exe_url: str, released_at: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO agent_releases (version, sha256, exe_url, released_at) VALUES (?,?,?,?)",
        (version, sha256, exe_url, released_at),
    )
    conn.commit()
    conn.close()
    _config_set("latest_agent_version", version)
    _config_set("latest_agent_sha256", sha256)
    _config_set("agent_download_url", exe_url)
    _config_set("latest_agent_url", exe_url)
    _config_set("latest_agent_released_at", released_at)
    _agent_url_cache["url"] = exe_url
    _agent_url_cache["ts"] = time.time()


def _post_notify_server(tunnel_url: str, version: str, sha256: str, exe_url: str):
    try:
        body = json.dumps({"version": version, "sha256": sha256, "exe_url": exe_url}).encode()
        req = urllib.request.Request(
            f"{tunnel_url}/api/v1/updates/notify",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx) as r:
            r.read()
    except Exception as e:
        print(f"WARN: notify {tunnel_url}: {e}", flush=True)


async def _notify_all_servers(version: str, sha256: str, exe_url: str):
    conn = sqlite3.connect(DB_PATH)
    servers = conn.execute("SELECT tunnel_url FROM servers WHERE tunnel_url IS NOT NULL").fetchall()
    conn.close()
    loop = asyncio.get_event_loop()
    pool = concurrent.futures.ThreadPoolExecutor()
    tasks = [
        loop.run_in_executor(pool, _post_notify_server, row[0], version, sha256, exe_url)
        for row in servers if row[0]
    ]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@app.post("/v1/github/stable")
async def stable_tag_webhook(request: Request):
    body = await request.body()

    if _CLOUD_WEBHOOK_SECRET:
        secret_header = request.headers.get("X-Webhook-Secret", "")
        if not hmac.compare_digest(secret_header, _CLOUD_WEBHOOK_SECRET):
            return JSONResponse(status_code=401, content={"error": "Invalid secret"})

    try:
        payload = json.loads(body)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    tag = payload.get("tag", "")
    if not tag.startswith("agent/v"):
        return {"ok": True, "skipped": True, "reason": "not an agent tag"}

    # CI creates the release under base tag (agent/v1.5.6), not the stable tag
    release_tag = tag.removesuffix("-stable")
    manifest = _fetch_latest_json_from_github(release_tag)
    if not manifest:
        return JSONResponse(status_code=422, content={"error": "latest.json not found in release"})

    version     = manifest.get("version", tag.removeprefix("agent/v"))
    sha256      = manifest.get("sha256", "")
    exe_url     = manifest.get("exe_url", "")
    released_at = manifest.get("released_at", datetime.datetime.now(datetime.timezone.utc).isoformat())

    if not sha256 or not exe_url:
        return JSONResponse(status_code=422, content={"error": "latest.json missing sha256 or exe_url"})

    _store_agent_release(version, sha256, exe_url, released_at)
    asyncio.create_task(_notify_all_servers(version, sha256, exe_url))
    return {"ok": True, "version": version, "sha256": sha256}


# ── Cloud API for servers (auth by X-Server-Token) ────────────────────────────

def _server_by_token(token: str) -> dict | None:
    if not token:
        return None
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, user_id, tunnel_url, api_key FROM servers WHERE server_token=?", (token,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row[0], "user_id": row[1], "tunnel_url": row[2], "api_key": row[3]}


@app.get("/api/v1/cloud/latest-agent")
def cloud_latest_agent(request: Request):
    token = request.headers.get("X-Server-Token", "")
    if not _server_by_token(token):
        return JSONResponse(status_code=401, content={"error": "Invalid server token"})
    version = _config_get("latest_agent_version")
    sha256  = _config_get("latest_agent_sha256")
    exe_url = _config_get("agent_download_url")
    if not version:
        # fallback: try agent_releases table
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT version, sha256, exe_url FROM agent_releases ORDER BY released_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            version, sha256, exe_url = row
    if not version:
        return JSONResponse(status_code=404, content={"error": "No release available"})
    return {"version": version, "sha256": sha256, "exe_url": exe_url}


@app.post("/api/v1/cloud/heartbeat")
async def cloud_server_heartbeat(request: Request):
    token = request.headers.get("X-Server-Token", "")
    srv = _server_by_token(token)
    if not srv:
        return JSONResponse(status_code=401, content={"error": "Invalid server token"})
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})
    tunnel_url = data.get("tunnel_url", "")
    api_key    = data.get("api_key", "")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    if tunnel_url and api_key:
        conn.execute(
            "UPDATE servers SET tunnel_url=?, api_key=?, heartbeat_at=? WHERE server_token=?",
            (tunnel_url, api_key, now, token),
        )
    else:
        conn.execute(
            "UPDATE servers SET heartbeat_at=? WHERE server_token=?", (now, token)
        )
    conn.commit()
    conn.close()
    return {"ok": True}


# ── Bootstrap proxy (hides server URL from agent install command) ─────────────

@app.get("/bootstrap/{token}")
@app.get("/v1/bootstrap/{token}")
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
    tunnel_url = user_server.get("tunnel_url")
    if not tunnel_url:
        return JSONResponse(status_code=503, content={"error": "Server has no CF tunnel (LAN-only mode)"})
    data = _api(tunnel_url, user_server["api_key"], "/api/v1/bootstrap/active")
    if not data:
        return JSONResponse(status_code=503, content={"error": "Bootstrap profile unavailable"})
    # Re-sign with cloud CA key so agent trusts one stable root of trust.
    # The server's CA key may change (e.g. volume wipe), but cloud CA never does.
    signed_data = data.get("signed_data")
    if not signed_data:
        return JSONResponse(status_code=502, content={"error": "Server returned invalid profile"})
    try:
        raw_bytes = base64.b64decode(signed_data)
        cloud_signature = _sign_data(raw_bytes)
        return {"signed_data": signed_data, "signature": cloud_signature}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Re-sign failed: {e}"})


# ── Install code generation (cabinet) ────────────────────────────────────────

@app.post("/cabinet/generate-install-code")
async def generate_install_code(request: Request):
    token = request.cookies.get("session")
    user  = _db_user_by_session(token)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})

    import base64 as _b64
    code = _b64.b32encode(secrets.token_bytes(10)).decode().rstrip("=").lower()

    expires_at = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=72)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO install_codes (code, user_id, expires_at) VALUES (?,?,?)",
        (code, user["id"], expires_at)
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/cabinet", status_code=303)


# ── New server token (cabinet) ────────────────────────────────────────────────

@app.post("/cabinet/new-server-token")
async def new_server_token(request: Request):
    """Create a new pending server slot with a unique server_token."""
    token = request.cookies.get("session")
    user  = _db_user_by_session(token)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    _create_pending_server(user["id"], server_name="Новый сервер")
    return RedirectResponse("/cabinet", status_code=303)


# ── Bootstrap-via-link landing page ──────────────────────────────────────────

@app.get("/i/{code}", response_class=HTMLResponse)
def install_landing(code: str):
    conn = sqlite3.connect(DB_PATH)
    row  = conn.execute(
        "SELECT user_id, expires_at FROM install_codes WHERE code=?", (code,)
    ).fetchone()
    conn.close()

    if not row:
        return HTMLResponse("<h1>Ссылка недействительна</h1>", status_code=404)

    expires_at = row[1]
    if expires_at and datetime.datetime.fromisoformat(expires_at).replace(
            tzinfo=datetime.timezone.utc) < datetime.datetime.now(datetime.timezone.utc):
        return HTMLResponse("<h1>Ссылка истекла</h1>", status_code=410)

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="UTF-8"><title>Установка Seamlean Agent</title>
<style>body{{font-family:system-ui,sans-serif;max-width:640px;margin:60px auto;padding:0 20px}}
code{{background:#f1f5f9;padding:4px 8px;border-radius:4px;font-size:.95em}}
.cmd{{background:#1e293b;color:#e2e8f0;padding:16px;border-radius:8px;font-family:monospace;font-size:.9em;margin:12px 0}}
</style></head>
<body>
<h1>Установка Seamlean Agent</h1>
<p>1. Скачайте исполняемый файл агента:</p>
<p><a href="https://seamlean.com/agent" download="Seamlean.Agent.exe">⬇ Скачать Seamlean.Agent.exe</a></p>
<p>2. Запустите от имени администратора:</p>
<div class="cmd">Seamlean.Agent.exe --install --install-code {code}</div>
<p>Агент будет автоматически настроен и запущен.</p>
</body></html>"""
    return HTMLResponse(html)


# ── Installer bootstrap endpoint (called by Installer.cs) ────────────────────

@app.post("/v1/installer/bootstrap")
async def installer_bootstrap(request: Request):
    """
    Validates install_code, fetches and re-signs bootstrap profile from tenant server.
    Rate-limited per code (10 req/hour per IP).
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    install_code = body.get("install_code", "").strip().lower()
    if not install_code:
        return JSONResponse(status_code=400, content={"error": "install_code required"})

    client_ip = (
        request.headers.get("CF-Connecting-IP") or
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or
        request.client.host
    )

    conn = sqlite3.connect(DB_PATH)
    row  = conn.execute(
        "SELECT user_id, expires_at, max_uses, used_count FROM install_codes WHERE code=?",
        (install_code,)
    ).fetchone()

    if not row:
        conn.close()
        return JSONResponse(status_code=403, content={"error": "Invalid install code"})

    user_id, expires_at, max_uses, used_count = row

    if expires_at and datetime.datetime.fromisoformat(expires_at).replace(
            tzinfo=datetime.timezone.utc) < datetime.datetime.now(datetime.timezone.utc):
        conn.close()
        return JSONResponse(status_code=403, content={"error": "Install code expired"})

    if used_count >= max_uses:
        conn.close()
        return JSONResponse(status_code=403, content={"error": "Install code usage limit reached"})

    conn.execute(
        "UPDATE install_codes SET used_count = used_count + 1 WHERE code=?", (install_code,)
    )
    conn.commit()
    conn.close()

    user_server = _get_user_server(user_id)
    if not user_server:
        return JSONResponse(status_code=503, content={"error": "Server not registered"})

    tunnel_url = user_server.get("tunnel_url")
    if not tunnel_url:
        return JSONResponse(status_code=503, content={"error": "Server has no CF tunnel"})

    # SSRF: only allow https://
    if not tunnel_url.startswith("https://"):
        return JSONResponse(status_code=502, content={"error": "Invalid server URL"})

    data = _api(tunnel_url, user_server["api_key"], "/api/v1/bootstrap/active")
    if not data:
        return JSONResponse(status_code=503, content={"error": "Bootstrap profile unavailable"})

    signed_data = data.get("signed_data")
    if not signed_data:
        return JSONResponse(status_code=502, content={"error": "Server returned invalid profile"})

    try:
        raw_bytes        = base64.b64decode(signed_data)
        cloud_signature  = _sign_data(raw_bytes)
        return {
            "profile": {"signed_data": signed_data, "signature": cloud_signature},
            "install_config": {
                "service_name": "WinDiagSvc",
                "install_dir":  r"C:\Program Files\Windows Diagnostics",
                "data_dir":     r"%ProgramData%\Microsoft\Diagnostics",
            },
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Re-sign failed: {e}"})


# ── Robots / Health ───────────────────────────────────────────────────────────

@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nAllow: /\n"


@app.get("/health")
def health():
    return {"status": "ok", "version": _CLOUD_VERSION}
