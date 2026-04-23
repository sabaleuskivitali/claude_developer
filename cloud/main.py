import hashlib
import json
import os
import re
import sqlite3
import ssl
import urllib.request
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Header, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel

app = FastAPI()

PUBLIC_URL   = os.environ["PUBLIC_URL"].rstrip("/")
DIAG_API     = os.environ.get("DIAG_API_URL", "https://api.seamlean.com")
DIAG_KEY     = os.environ.get("DIAG_API_KEY", "")
PROFILES_DIR = Path("/app/profiles")
PROFILES_DIR.mkdir(exist_ok=True)
DB_PATH      = Path("/app/users.db")

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode    = ssl.CERT_NONE

BOOTSTRAP_URL = f"{DIAG_API}/api/v1/bootstrap/active"


# ── DB ────────────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        pwd_hash TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.commit()
    conn.close()


init_db()


# ── Auth helpers ──────────────────────────────────────────────────────────────

def get_user_by_session(token: str):
    if not token:
        return None
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT u.id, u.email FROM users u JOIN sessions s ON u.id=s.user_id WHERE s.token=?",
        (token,)
    ).fetchone()
    conn.close()
    return {"id": row[0], "email": row[1]} if row else None


def hash_pwd(pwd: str) -> str:
    return hashlib.sha256(pwd.encode()).hexdigest()


def create_session(user_id: str) -> str:
    token = str(uuid.uuid4())
    conn  = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO sessions(token,user_id) VALUES(?,?)", (token, user_id))
    conn.commit()
    conn.close()
    return token


# ── Diag API helpers ──────────────────────────────────────────────────────────

def _api_get(path: str):
    try:
        req = urllib.request.Request(
            f"{DIAG_API}{path}",
            headers={"X-Api-Key": DIAG_KEY},
        )
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return None


def fetch_agents():
    data = _api_get("/api/v1/agents")
    if not data:
        return []
    return data.get("agents", [])


# ── HTML template ─────────────────────────────────────────────────────────────

def _page(title: str, body: str, wide: bool = False) -> HTMLResponse:
    max_w = "860px" if wide else "480px"
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Seamlean{title}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;color:#222;min-height:100vh}}
  .wrap{{max-width:{max_w};margin:0 auto;padding:40px 16px}}
  .card{{background:#fff;border-radius:12px;padding:32px;box-shadow:0 2px 12px rgba(0,0,0,.07)}}
  h1{{font-size:1.6rem;font-weight:700;margin-bottom:6px}}
  .sub{{color:#666;font-size:.9rem;margin-bottom:24px}}
  label{{display:block;font-size:.84rem;font-weight:600;margin-bottom:4px;color:#444}}
  input[type=email],input[type=password]{{width:100%;padding:10px 12px;border:1.5px solid #ddd;border-radius:8px;font-size:1rem;outline:none;transition:border .15s}}
  input:focus{{border-color:#4f46e5}}
  .btn{{display:inline-block;padding:10px 20px;background:#4f46e5;color:#fff;border:none;border-radius:8px;font-size:.95rem;font-weight:600;cursor:pointer;text-decoration:none;transition:opacity .15s}}
  .btn:hover{{opacity:.88}}
  .btn-block{{display:block;width:100%;text-align:center;margin-top:16px}}
  .btn-green{{background:#059669}}
  .btn-gray{{background:#e5e7eb;color:#374151}}
  .link{{text-align:center;margin-top:14px;font-size:.88rem;color:#666}}
  .link a{{color:#4f46e5;text-decoration:none;font-weight:600}}
  .err{{color:#dc2626;font-size:.84rem;margin-top:8px;padding:8px 12px;background:#fef2f2;border-radius:6px}}
  .badge{{display:inline-block;padding:2px 8px;border-radius:20px;font-size:.76rem;font-weight:700}}
  .online{{background:#d1fae5;color:#065f46}}
  .warning{{background:#fef3c7;color:#92400e}}
  .offline{{background:#fee2e2;color:#991b1b}}
  .section{{margin-top:28px}}
  .section-title{{font-size:1rem;font-weight:700;margin-bottom:10px;color:#374151}}
  table{{width:100%;border-collapse:collapse;font-size:.86rem}}
  th{{background:#f9fafb;padding:7px 10px;text-align:left;font-weight:600;color:#6b7280;font-size:.78rem;text-transform:uppercase;letter-spacing:.04em;border-bottom:1px solid #e5e7eb}}
  td{{padding:8px 10px;border-bottom:1px solid #f3f4f6;vertical-align:middle}}
  tr:last-child td{{border-bottom:none}}
  .code-box{{background:#f3f4f6;border-radius:8px;padding:10px 14px;font-family:monospace;font-size:.83rem;word-break:break-all;margin:10px 0}}
  .copy-row{{display:flex;gap:8px;align-items:center}}
  .copy-row .code-box{{flex:1;margin:0}}
  .layer-ok{{background:#d1fae5;color:#065f46;padding:1px 5px;border-radius:4px;font-size:.74rem;font-weight:600;margin-right:2px}}
  .layer-err{{background:#fee2e2;color:#991b1b;padding:1px 5px;border-radius:4px;font-size:.74rem;font-weight:600;margin-right:2px}}
  .nav{{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}}
  .nav-brand{{font-weight:700;font-size:1.05rem}}
  .tabs{{display:flex;border-bottom:2px solid #e5e7eb;margin-bottom:24px;gap:0}}
  .tab{{padding:8px 18px;cursor:pointer;font-size:.92rem;font-weight:500;color:#6b7280;background:none;border:none;border-bottom:2px solid transparent;margin-bottom:-2px;transition:color .15s}}
  .tab.active{{color:#4f46e5;border-bottom-color:#4f46e5;font-weight:700}}
  .panel{{display:none}}.panel.active{{display:block}}
</style>
</head>
<body>
<div class="wrap">
{body}
</div>
<script>
function copyText(id){{
  var el=document.getElementById(id);
  navigator.clipboard.writeText(el.innerText).then(function(){{
    var b=el.nextElementSibling;
    var orig=b.textContent;b.textContent='Скопировано!';
    setTimeout(function(){{b.textContent=orig}},2000);
  }});
}}
function showTab(name){{
  document.querySelectorAll('.tab').forEach(function(t){{t.classList.toggle('active',t.dataset.tab===name)}});
  document.querySelectorAll('.panel').forEach(function(p){{p.classList.toggle('active',p.id==='panel-'+name)}});
}}
</script>
</body>
</html>""")


# ── Landing / redirect ────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    token = request.cookies.get("session")
    if get_user_by_session(token):
        return RedirectResponse("/cabinet", status_code=302)
    return RedirectResponse("/login", status_code=302)


# ── Login ─────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def login_page(err: str = ""):
    err_html = f'<div class="err">{err}</div>' if err else ""
    return _page("", f"""
<div class="card">
  <h1>Seamlean</h1>
  <p class="sub">Войдите в личный кабинет</p>
  {err_html}
  <form method="post" action="/login">
    <label>Email</label>
    <input type="email" name="email" required autofocus placeholder="you@company.com">
    <label style="margin-top:14px">Пароль</label>
    <input type="password" name="password" required placeholder="••••••••">
    <button class="btn btn-block" type="submit">Войти</button>
  </form>
  <p class="link"><a href="/register">Нет аккаунта? Зарегистрироваться</a></p>
</div>""")


@app.post("/login", response_class=HTMLResponse)
def login_post(email: str = Form(...), password: str = Form(...)):
    conn = sqlite3.connect(DB_PATH)
    row  = conn.execute(
        "SELECT id FROM users WHERE email=? AND pwd_hash=?",
        (email.lower().strip(), hash_pwd(password))
    ).fetchone()
    conn.close()
    if not row:
        return RedirectResponse("/login?err=Неверный+email+или+пароль", status_code=302)
    token = create_session(row[0])
    resp  = RedirectResponse("/cabinet", status_code=302)
    resp.set_cookie("session", token, httponly=True, samesite="lax")
    return resp


# ── Register ──────────────────────────────────────────────────────────────────

@app.get("/register", response_class=HTMLResponse)
def register_page(err: str = ""):
    err_html = f'<div class="err">{err}</div>' if err else ""
    return _page(" — Регистрация", f"""
<div class="card">
  <h1>Seamlean</h1>
  <p class="sub">Создайте аккаунт</p>
  {err_html}
  <form method="post" action="/register">
    <label>Email</label>
    <input type="email" name="email" required autofocus placeholder="you@company.com">
    <label style="margin-top:14px">Пароль</label>
    <input type="password" name="password" required minlength="6" placeholder="Минимум 6 символов">
    <button class="btn btn-block" type="submit">Зарегистрироваться</button>
  </form>
  <p class="link"><a href="/login">Уже есть аккаунт? Войти</a></p>
</div>""")


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
        conn.execute("INSERT INTO users(id,email,pwd_hash) VALUES(?,?,?)", (uid, email, hash_pwd(password)))
        conn.commit()
        conn.close()
    except sqlite3.IntegrityError:
        return RedirectResponse("/register?err=Email+уже+зарегистрирован", status_code=302)
    token = create_session(uid)
    resp  = RedirectResponse("/cabinet", status_code=302)
    resp.set_cookie("session", token, httponly=True, samesite="lax")
    return resp


# ── Cabinet ───────────────────────────────────────────────────────────────────

_LAYER_LABELS = {
    "window":  "Окна",
    "visual":  "Скрины",
    "system":  "Система",
    "applogs": "Логи",
    "browser": "Браузер",
}

_STATUS_ICONS = {"online": "🟢", "warning": "🟡", "offline": "🔴"}


def _layers_html(layer_stats) -> str:
    if not layer_stats:
        return '<span style="color:#9ca3af;font-size:.8rem">—</span>'
    try:
        stats = json.loads(layer_stats) if isinstance(layer_stats, str) else layer_stats
    except Exception:
        return "—"
    parts = []
    for key, label in _LAYER_LABELS.items():
        ls = stats.get(key)
        if ls is None:
            continue
        errors = ls.get("errors_5min", 0) or 0
        cls    = "layer-err" if errors > 0 else "layer-ok"
        parts.append(f'<span class="{cls}">{label}</span>')
    return "".join(parts) if parts else "—"


def _agent_rows(agents) -> str:
    if not agents:
        return '<tr><td colspan="5" style="text-align:center;color:#9ca3af;padding:20px">Агентов нет. Установите первого с помощью ссылки выше.</td></tr>'
    rows = ""
    for a in agents:
        lag   = a.get("lag_sec", 0)
        drift = a.get("drift_ms")
        st    = a.get("status", "offline")
        icon  = _STATUS_ICONS.get(st, "❓")
        if lag < 60:
            last = f"{lag}с назад"
        elif lag < 3600:
            last = f"{lag // 60}м назад"
        else:
            last = f"{lag // 3600}ч назад"
        drift_html = f'<span style="color:{"#dc2626" if drift and abs(drift)>1000 else "#374151"}">{drift:+d}мс</span>' if drift is not None else "—"
        rows += f"""<tr>
          <td style="font-family:monospace;font-size:.78rem">{a.get('machine_id','')[:12]}…</td>
          <td><span class="badge {st}">{icon} {st}</span></td>
          <td style="color:#6b7280">{last}</td>
          <td>{_layers_html(a.get('layer_stats'))}</td>
          <td>{drift_html}</td>
        </tr>"""
    return rows


@app.get("/cabinet", response_class=HTMLResponse)
def cabinet(request: Request):
    token = request.cookies.get("session")
    user  = get_user_by_session(token)
    if not user:
        return RedirectResponse("/login", status_code=302)

    agents = fetch_agents()

    agent_rows = _agent_rows(agents)
    agent_count = len(agents)
    online_count = sum(1 for a in agents if a.get("status") == "online")

    body = f"""
<div class="nav">
  <span class="nav-brand">Seamlean</span>
  <span style="display:flex;align-items:center;gap:12px">
    <span style="font-size:.84rem;color:#6b7280">{user['email']}</span>
    <form method="post" action="/logout" style="display:inline">
      <button class="btn btn-gray" style="padding:6px 14px;font-size:.82rem">Выйти</button>
    </form>
  </span>
</div>

<div class="tabs">
  <button class="tab active" data-tab="server" onclick="showTab('server')">Сервер</button>
  <button class="tab" data-tab="agents" onclick="showTab('agents')">
    Агенты
    {"&nbsp;<span style='background:#4f46e5;color:#fff;border-radius:20px;padding:1px 7px;font-size:.75rem'>" + str(online_count) + "/" + str(agent_count) + "</span>" if agent_count > 0 else ""}
  </button>
</div>

<div id="panel-server" class="panel active">
  <div class="card">
    <div class="section-title">Облачный сервер</div>
    <table>
      <tr><td style="color:#6b7280;width:140px">Адрес</td><td><code>api.seamlean.com</code></td></tr>
      <tr><td style="color:#6b7280">Статус</td><td><span class="badge online">🟢 Онлайн</span></td></tr>
      <tr><td style="color:#6b7280">Режим</td><td>Cloud</td></tr>
    </table>
  </div>
</div>

<div id="panel-agents" class="panel">
  <div class="card" style="margin-bottom:16px">
    <div class="section-title">Bootstrap-ссылка для установки агента</div>
    <p style="font-size:.86rem;color:#6b7280;margin-bottom:10px">
      Отправьте ссылку пользователям или запустите на Windows-машине:
    </p>
    <div class="copy-row">
      <div class="code-box" id="burl">{BOOTSTRAP_URL}</div>
      <button class="btn btn-gray" style="padding:8px 14px;font-size:.82rem;white-space:nowrap" onclick="copyText('burl')">Скопировать</button>
    </div>
    <p style="font-size:.78rem;color:#9ca3af;margin-top:8px">
      PowerShell: <code>powershell -ExecutionPolicy Bypass -File install.ps1</code>
    </p>
  </div>

  <div class="card">
    <div class="section-title">Устройства</div>
    <table>
      <thead><tr>
        <th>Machine ID</th><th>Статус</th><th>Last seen</th>
        <th>Слои</th><th>Drift</th>
      </tr></thead>
      <tbody>{agent_rows}</tbody>
    </table>
  </div>
</div>"""

    return _page(" — Кабинет", body, wide=True)


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


# ── Bootstrap upload/download (existing, for cloud publisher) ─────────────────

class SignedBootstrapProfile(BaseModel):
    signed_data: str
    signature:   str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/upload")
def upload(profile: SignedBootstrapProfile, x_api_key: str = Header(...)):
    api_key = os.environ.get("CLOUD_API_KEY", "")
    if x_api_key != api_key:
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = str(uuid.uuid4())
    (PROFILES_DIR / f"{token}.json").write_text(profile.model_dump_json())
    return {"url": f"{PUBLIC_URL}/p/{token}"}


@app.get("/p/{token}")
def download(token: str):
    if not re.fullmatch(r"[0-9a-f-]{36}", token):
        raise HTTPException(status_code=400, detail="Invalid token")
    path = PROFILES_DIR / f"{token}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return JSONResponse(content=json.loads(path.read_text()))
