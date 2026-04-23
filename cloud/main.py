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

API_KEY     = os.environ["CLOUD_API_KEY"]
PUBLIC_URL  = os.environ["PUBLIC_URL"].rstrip("/")
DIAG_API    = os.environ.get("DIAG_API_URL", "https://195.222.86.67:49200")
DIAG_KEY    = os.environ.get("DIAG_API_KEY", "")
PROFILES_DIR = Path("/app/profiles")
PROFILES_DIR.mkdir(exist_ok=True)
DB_PATH     = Path("/app/users.db")

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


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


def fetch_agent_statuses():
    try:
        req = urllib.request.Request(
            f"{DIAG_API}/api/v1/agents",
            headers={"X-Api-Key": DIAG_KEY},
        )
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return []


def get_latest_release_url():
    return "https://github.com/sabaleuskivitali/claude_developer/releases/latest"


HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Seamlean{title_suffix}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;color:#222}}
  .wrap{{max-width:480px;margin:80px auto;padding:0 16px}}
  .card{{background:#fff;border-radius:12px;padding:40px;box-shadow:0 2px 16px rgba(0,0,0,.08)}}
  h1{{font-size:1.6rem;font-weight:700;margin-bottom:8px}}
  .sub{{color:#666;font-size:.9rem;margin-bottom:28px}}
  label{{display:block;font-size:.85rem;font-weight:600;margin-bottom:4px;color:#444}}
  input[type=email],input[type=password]{{width:100%;padding:10px 12px;border:1.5px solid #ddd;border-radius:8px;font-size:1rem;outline:none;transition:border .2s}}
  input:focus{{border-color:#4f46e5}}
  .btn{{display:block;width:100%;padding:12px;background:#4f46e5;color:#fff;border:none;border-radius:8px;font-size:1rem;font-weight:600;cursor:pointer;margin-top:20px;transition:background .2s}}
  .btn:hover{{background:#4338ca}}
  .link{{text-align:center;margin-top:16px;font-size:.9rem;color:#666}}
  .link a{{color:#4f46e5;text-decoration:none;font-weight:600}}
  .err{{color:#dc2626;font-size:.85rem;margin-top:8px;text-align:center}}
  .wide{{max-width:820px}}
  table{{width:100%;border-collapse:collapse;font-size:.88rem;margin-top:8px}}
  th{{background:#f3f4f6;padding:8px 12px;text-align:left;font-weight:600;color:#555}}
  td{{padding:8px 12px;border-bottom:1px solid #f0f0f0}}
  .badge{{display:inline-block;padding:2px 8px;border-radius:20px;font-size:.78rem;font-weight:600}}
  .online{{background:#d1fae5;color:#065f46}}
  .offline{{background:#fee2e2;color:#991b1b}}
  .warning{{background:#fef3c7;color:#92400e}}
  .dl-btn{{display:inline-block;padding:12px 24px;background:#059669;color:#fff;border-radius:8px;font-weight:600;text-decoration:none;font-size:.95rem}}
  .dl-btn:hover{{background:#047857}}
  .section{{margin-top:32px}}
  .section h2{{font-size:1.1rem;font-weight:700;margin-bottom:12px;color:#374151}}
  .logout{{float:right;font-size:.82rem;color:#9ca3af;text-decoration:none}}
  .logout:hover{{color:#4f46e5}}
</style>
</head>
<body>
<div class="wrap {wide_class}">
<div class="card">
{body}
</div>
</div>
</body>
</html>"""


def render(title_suffix, wide_class, body):
    return HTMLResponse(HTML.format(title_suffix=title_suffix, wide_class=wide_class, body=body))


# ── Landing ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    token = request.cookies.get("session")
    if get_user_by_session(token):
        return RedirectResponse("/cabinet", status_code=302)
    return RedirectResponse("/login", status_code=302)


# ── Login ─────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def login_page(err: str = ""):
    err_html = f'<p class="err">{err}</p>' if err else ""
    body = f"""
    <h1>Seamlean</h1>
    <p class="sub">Войдите в личный кабинет</p>
    <form method="post" action="/login">
      <label>Email</label>
      <input type="email" name="email" required autofocus>
      <label style="margin-top:16px">Пароль</label>
      <input type="password" name="password" required>
      <button class="btn" type="submit">Войти</button>
      {err_html}
    </form>
    <p class="link"><a href="/register">Нет аккаунта? Зарегистрироваться</a></p>"""
    return render("", "", body)


@app.post("/login", response_class=HTMLResponse)
def login_post(email: str = Form(...), password: str = Form(...)):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id FROM users WHERE email=? AND pwd_hash=?",
        (email.lower().strip(), hash_pwd(password))
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


# ── Register ──────────────────────────────────────────────────────────────────

@app.get("/register", response_class=HTMLResponse)
def register_page(err: str = ""):
    err_html = f'<p class="err">{err}</p>' if err else ""
    body = f"""
    <h1>Seamlean</h1>
    <p class="sub">Создайте аккаунт</p>
    <form method="post" action="/register">
      <label>Email</label>
      <input type="email" name="email" required autofocus>
      <label style="margin-top:16px">Пароль</label>
      <input type="password" name="password" required minlength="6">
      <button class="btn" type="submit">Зарегистрироваться</button>
      {err_html}
    </form>
    <p class="link"><a href="/login">Уже есть аккаунт? Войти</a></p>"""
    return render(" — Регистрация", "", body)


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
    token = str(uuid.uuid4())
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO sessions(token,user_id) VALUES(?,?)", (token, uid))
    conn.commit()
    conn.close()
    resp = RedirectResponse("/cabinet", status_code=302)
    resp.set_cookie("session", token, httponly=True, samesite="lax")
    return resp


# ── Cabinet ───────────────────────────────────────────────────────────────────

@app.get("/cabinet", response_class=HTMLResponse)
def cabinet(request: Request):
    token = request.cookies.get("session")
    user = get_user_by_session(token)
    if not user:
        return RedirectResponse("/login", status_code=302)

    release_url = get_latest_release_url()
    agents = fetch_agent_statuses()

    def status_badge(s):
        cls = {"online": "online", "warning": "warning"}.get(s, "offline")
        icons = {"online": "🟢", "warning": "🟡", "offline": "🔴", "never": "❓"}
        return f'<span class="badge {cls}">{icons.get(s,"?")} {s}</span>'

    rows = ""
    if agents:
        for a in agents:
            layers = a.get("layers", {})
            layer_html = " ".join(
                f'<span title="{k}" style="font-size:.8rem">{"✅" if v else "❌"}{k[:3]}</span>'
                for k, v in layers.items()
            ) if layers else "—"
            rows += f"""<tr>
              <td style="font-family:monospace;font-size:.78rem">{a.get('machine_id','')[:12]}…</td>
              <td>{status_badge(a.get('status','offline'))}</td>
              <td>{a.get('last_seen','—')}</td>
              <td>{layer_html}</td>
              <td>{a.get('drift_ms','—')}</td>
              <td>{a.get('sync_lag_sec','—')}</td>
            </tr>"""
    else:
        rows = '<tr><td colspan="6" style="text-align:center;color:#9ca3af;padding:20px">Агенты не зарегистрированы</td></tr>'

    body = f"""
    <a class="logout" href="/logout">Выйти</a>
    <h1>Личный кабинет</h1>
    <p class="sub">{user['email']}</p>

    <div class="section">
      <h2>Установка агента</h2>
      <p style="font-size:.9rem;color:#555;margin-bottom:16px">
        Скачайте инсталлятор и запустите от имени администратора на Windows-машине:
      </p>
      <a class="dl-btn" href="{release_url}" target="_blank">⬇ Скачать агента (последний релиз)</a>
      <p style="font-size:.8rem;color:#9ca3af;margin-top:10px">
        PowerShell: <code>powershell -ExecutionPolicy Bypass -File Install-WinDiagSvc.ps1</code>
      </p>
    </div>

    <div class="section">
      <h2>Статус агентов</h2>
      <table>
        <thead><tr>
          <th>Machine ID</th><th>Статус</th><th>Последний раз</th>
          <th>Слои</th><th>Drift ms</th><th>Sync lag</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""
    return render(" — Кабинет", "wide", body)


@app.get("/logout")
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


# ── Bootstrap API (existing) ──────────────────────────────────────────────────

class SignedBootstrapProfile(BaseModel):
    signed_data: str
    signature: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/upload")
def upload(profile: SignedBootstrapProfile, x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
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
