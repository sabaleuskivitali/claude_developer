import base64
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
    conn.execute("""CREATE TABLE IF NOT EXISTS servers (
        user_id TEXT PRIMARY KEY,
        server_url TEXT NOT NULL,
        tunnel_url TEXT,
        api_key TEXT NOT NULL,
        server_name TEXT DEFAULT 'Мой сервер',
        registered_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS config (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""")
    conn.commit()
    # migrate: add install_token if column missing
    try:
        conn.execute("ALTER TABLE users ADD COLUMN install_token TEXT")
        conn.commit()
    except Exception:
        pass
    for col in ["tunnel_url TEXT", "wan_url TEXT", "lan_url TEXT", "cf_tunnel_id TEXT", "heartbeat_at TEXT"]:
        try:
            conn.execute(f"ALTER TABLE servers ADD COLUMN {col}")
            conn.commit()
        except Exception:
            pass
    conn.close()


init_db()


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


def _get_user_server(user_id: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT server_url, wan_url, lan_url, api_key, server_name, heartbeat_at, tunnel_url FROM servers WHERE user_id=?",
        (user_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "server_url":   row[0],
        "wan_url":      row[1],   # public IP — display only
        "lan_url":      row[2],
        "api_key":      row[3],
        "server_name":  row[4],
        "heartbeat_at": row[5],
        "tunnel_url":   row[6],   # CF tunnel URL — health checks only
    }


def _is_cf_url(url: str) -> bool:
    """CF tunnel URLs and custom domains are safe to health-check; raw IP:port URLs are not."""
    return bool(url) and not re.match(r'https?://\d+\.\d+\.\d+\.\d+', url)


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
        row  = conn.execute("SELECT id FROM users WHERE install_token=?", (install_token,)).fetchone()
        if not row:
            conn.close()
            return {"ok": False, "error": "Invalid token"}

        user_id  = row[0]
        existing = conn.execute(
            "SELECT api_key, cf_tunnel_id, tunnel_url FROM servers WHERE user_id=?", (user_id,)
        ).fetchone()

        api_key = existing[0] if existing else uuid.uuid4().hex + uuid.uuid4().hex

        cf_tunnel_id = existing[1] if existing else None
        tunnel_token = None
        cf_url       = existing[2] if existing else None  # preserve stored tunnel_url

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
               (user_id, server_url, lan_url, wan_url, tunnel_url, cf_tunnel_id, api_key, server_name, heartbeat_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (user_id, lan_url or server_url, lan_url or None, wan_url, cf_url, cf_tunnel_id, api_key, server_name, now)
        )
        conn.commit()
        conn.close()
        return {"ok": True, "api_key": api_key, "tunnel_token": tunnel_token, "tunnel_url": cf_url}
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


def _parse_layer_stats(layer_stats):
    try:
        stats = json.loads(layer_stats) if isinstance(layer_stats, str) else (layer_stats or {})
        # Normalise PascalCase keys from C# agent to snake_case
        return {
            layer: {
                "events_5min": (ls.get("Events5Min") or ls.get("events_5min") or 0),
                "errors_5min": (ls.get("Errors5Min") or ls.get("errors_5min") or 0),
            }
            for layer, ls in stats.items() if ls
        }
    except Exception:
        return {}


def _collection_badge(stats: dict) -> str:
    if not stats:
        return '<span style="color:#9ca3af;font-size:.82rem">нет данных</span>'
    has_errors = any((ls.get("errors_5min") or 0) > 0 for ls in stats.values() if ls)
    active = sum(1 for ls in stats.values() if ls and (ls.get("events_5min") or 0) > 0)
    if has_errors:
        return '<span style="background:#fee2e2;color:#991b1b;padding:2px 9px;border-radius:12px;font-size:.78rem;font-weight:600">⚠ Ошибки</span>'
    if active == 0:
        return '<span style="color:#9ca3af;font-size:.82rem">нет событий</span>'
    return '<span style="background:#d1fae5;color:#065f46;padding:2px 9px;border-radius:12px;font-size:.78rem;font-weight:600">✅ Работает</span>'


def _layers_detail_row(row_id: str, stats: dict) -> str:
    cells = ""
    for key, label in _LAYER_LABELS.items():
        ls     = stats.get(key)
        events = (ls.get("events_5min") or 0) if ls else None
        errors = (ls.get("errors_5min") or 0) if ls else None
        if ls is None:
            icon, col, detail = "—", "#9ca3af", "нет данных"
        elif errors:
            icon, col, detail = "⚠", "#991b1b", f"{events} / <b style='color:#991b1b'>{errors} ошибок</b>"
        elif events:
            icon, col, detail = "✅", "#065f46", f"{events} соб."
        else:
            icon, col, detail = "○", "#9ca3af", "0 соб."
        cells += (f'<td style="padding:4px 12px 4px 0;white-space:nowrap">'
                  f'<span style="color:{col};font-size:.8rem">{icon} {label}</span>'
                  f'<span style="color:#9ca3af;font-size:.75rem;margin-left:4px">{detail}</span></td>')
    return (f'<tr id="layer-{row_id}" style="display:none;background:#f8fafc">'
            f'<td colspan="6" style="padding:6px 16px 10px 32px">'
            f'<table style="border:0"><tr>{cells}</tr></table></td></tr>')


def _agent_rows(agents) -> str:
    if not agents:
        return '<tr><td colspan="6" style="text-align:center;color:#9ca3af;padding:20px">Агентов нет. Установите первого с помощью команды выше.</td></tr>'
    rows = ""
    for i, a in enumerate(agents):
        lag   = a.get("lag_sec", 0)
        drift = a.get("drift_ms")
        st    = a.get("status", "offline")
        ver   = a.get("agent_version") or "—"
        last  = f"{lag}с" if lag < 60 else (f"{lag//60}м" if lag < 3600 else f"{lag//3600}ч")
        drift_s = (f'<span style="color:{"#dc2626" if abs(drift)>1000 else "#374151"}">{drift:+d}мс</span>'
                   if drift is not None else "—")
        stats = _parse_layer_stats(a.get("layer_stats"))
        rid   = f"a{i}"
        rows += f"""<tr style="cursor:pointer" onclick="toggleLayer('{rid}')">
          <td style="font-family:monospace;font-size:.78rem">{a.get('machine_id','')[:12]}…</td>
          <td><span class="badge {st}">{_ST_ICON.get(st,'❓')} {st}</span></td>
          <td style="color:#6b7280;font-size:.82rem">{last} назад</td>
          <td style="color:#6b7280;font-size:.82rem">{ver}</td>
          <td>{_collection_badge(stats)}</td>
          <td>{drift_s}</td>
        </tr>"""
        rows += _layers_detail_row(rid, stats)
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
        wan_url      = user_server.get("wan_url")    # public IP — display only
        tunnel_url   = user_server.get("tunnel_url")  # CF URL — health checks
        api_key      = user_server["api_key"]
        hb_age, hb_s = _heartbeat_age(user_server.get("heartbeat_at"))

        # Always fetch health + agents via CF tunnel (never via raw public IP).
        # Heartbeat is used only as fallback status when tunnel is unreachable.
        _STALE = 600
        hb_stale = hb_age is None or hb_age >= _STALE
        if tunnel_url:
            health      = _api(tunnel_url, api_key, "/health")
            agents_data = (_api(tunnel_url, api_key, "/api/v1/agents") or {}).get("agents", [])
        else:
            health      = None
            agents_data = []

        _na = '<span style="color:#9ca3af">недоступно — нет туннеля</span>'
        extra = ""
        if health and health.get("status") in ("ok", "degraded"):
            # Stale heartbeat but health check succeeded
            status_badge = f'<span class="badge online">🟢 Онлайн{hb_s}</span>'
            if health.get("version"):
                extra += f'<tr><td style="color:#6b7280">Версия</td><td>{health["version"]}</td></tr>'
            if health.get("db"):
                db_ok = health["db"] == "ok"
                db_size = f' — {health["db_size_mb"]} MB' if health.get("db_size_mb") is not None else ""
                extra += f'<tr><td style="color:#6b7280">База данных</td><td>{"✅" if db_ok else "⚠️"} {health["db"]}{db_size}</td></tr>'
            if health.get("disk_free_gb") is not None:
                extra += f'<tr><td style="color:#6b7280">Диск свободно</td><td>{health["disk_free_gb"]} GB</td></tr>'
        elif hb_age is not None and hb_age < _STALE and not tunnel_url:
            status_badge = f'<span class="badge online">🟢 Онлайн{hb_s}</span>'
            extra = (f'<tr><td style="color:#6b7280">Версия</td><td>{_na}</td></tr>'
                     f'<tr><td style="color:#6b7280">Диск свободно</td><td>{_na}</td></tr>')
        elif hb_age is not None and hb_age < _STALE:
            # Fresh heartbeat — server is alive
            status_badge = f'<span class="badge online">🟢 Онлайн{hb_s}</span>'
        elif hb_age is not None and health is None and hb_stale:
            # Stale heartbeat + health check failed
            status_badge = f'<span class="badge offline">🔴 Недоступен{hb_s}</span>'
        elif hb_age is not None:
            status_badge = f'<span class="badge offline">🔴 Недоступен{hb_s}</span>'
        else:
            status_badge = '<span class="badge warning">⚠️ Ожидание сервера</span>'

        lan_url  = user_server.get("lan_url") or user_server["server_url"]
        wan_row  = (f'<tr><td style="color:#6b7280">WAN адрес</td><td><code>{wan_url}</code></td></tr>'
                    if wan_url else "")

        server_panel = f"""
  <div class="card">
    <div class="sec-title">{user_server['server_name']}</div>
    <table>
      <tr><td style="color:#6b7280;width:160px">LAN адрес</td><td><code>{lan_url}</code></td></tr>
      {wan_row}
      <tr><td style="color:#6b7280">Статус</td><td>{status_badge}</td></tr>
      {extra}
    </table>
  </div>"""
        bootstrap_url = user_server["server_url"]
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
        <th>Machine ID</th><th>Агент</th><th>Last seen</th><th>Версия</th><th>Сбор данных</th><th>Drift</th>
      </tr></thead>
      <tbody>{_agent_rows(agents_data)}</tbody>
    </table>
    <p style="color:#9ca3af;font-size:.78rem;margin-top:8px">↕ Нажмите на строку чтобы увидеть статус слоёв</p>
  </div>
</div>
<script>
function toggleLayer(id) {{
  var row = document.getElementById('layer-' + id);
  if (row) row.style.display = row.style.display === 'none' ? 'table-row' : 'none';
}}
</script>"""

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

    # ── Install cloudflared for WAN access ──────────────────────────────────
    if [ -n "$TUNNEL_TOKEN" ]; then
      echo "Installing cloudflared for WAN tunnel..."
      if ! command -v cloudflared &>/dev/null; then
        curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
          | gpg --dearmor -o /usr/share/keyrings/cloudflare-main.gpg
        echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
          > /etc/apt/sources.list.d/cloudflared.list
        apt-get update -q && apt-get install -y -q cloudflared
      fi
      cloudflared service uninstall 2>/dev/null || true
      systemctl stop cloudflared 2>/dev/null || true
      cloudflared service install "$TUNNEL_TOKEN"
      systemctl enable --now cloudflared
      echo "✅ Cloudflare tunnel active — WAN access enabled."
    fi

    # Restart API with correct key
    docker compose up -d api
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


# ── Robots / Health ───────────────────────────────────────────────────────────

@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nAllow: /\n"


@app.get("/health")
def health():
    return {"status": "ok", "version": _CLOUD_VERSION}
