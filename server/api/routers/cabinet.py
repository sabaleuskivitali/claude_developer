"""
Cabinet router — web UI for users (CJM steps 1–7 MVP).
Routes:
  GET  /           → landing
  GET  /register   → registration form
  POST /register   → create account → auto-login → /cabinet
  GET  /login      → login form
  POST /login      → authenticate → /cabinet
  POST /logout     → clear cookie → /login
  GET  /cabinet    → dashboard (server status + agents + bootstrap link)
"""
import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import auth_web
from bootstrap import store as bootstrap_store
from bootstrap.generator import generate_profile
from bootstrap.scanner import build_context

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ── helpers ───────────────────────────────────────────────────────────────────

def _redirect(url: str):
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


async def _ensure_bootstrap_profile(db):
    """Return active bootstrap URL, creating and publishing a profile if none exists."""
    signed = await bootstrap_store.get_active(db)
    if signed:
        return signed

    ctx = build_context(
        server_url="https://api.seamlean.com",
        tenant_id="default",
        site_id="default",
    )
    profile = generate_profile(ctx)
    profile_id = await bootstrap_store.create(db, profile, ctx)

    p = profile.get_profile()
    token = p.enrollment.token
    from datetime import datetime, timezone
    expires_at = datetime.fromisoformat(p.enrollment.expires_at).astimezone(timezone.utc)
    await db.execute(
        "INSERT INTO enrollment_tokens (token, profile_id, expires_at) VALUES ($1, $2::UUID, $3)",
        token, profile_id, expires_at,
    )
    await bootstrap_store.approve(db, profile_id)
    await bootstrap_store.publish(db, profile_id)
    return await bootstrap_store.get_active(db)


def _layer_stats(layer_stats_json: str | None) -> dict:
    if not layer_stats_json:
        return {}
    try:
        return json.loads(layer_stats_json)
    except Exception:
        return {}


# ── public pages ──────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse("landing.html", {"request": request})


@router.get("/register", response_class=HTMLResponse)
async def register_form(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "error": None})


@router.post("/register", response_class=HTMLResponse)
async def register_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    email = email.strip().lower()
    if not email or not password or len(password) < 6:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Email и пароль обязательны (мин. 6 символов)"},
            status_code=400,
        )

    existing = await request.app.state.db.fetchrow(
        "SELECT id FROM users WHERE email = $1", email
    )
    if existing:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Email уже зарегистрирован"},
            status_code=400,
        )

    pw_hash = auth_web.hash_password(password)
    row = await request.app.state.db.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, $2) RETURNING id",
        email, pw_hash,
    )

    response = _redirect("/cabinet")
    auth_web.set_cookie(response, row["id"])
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    email = email.strip().lower()
    row = await request.app.state.db.fetchrow(
        "SELECT id, password_hash FROM users WHERE email = $1", email
    )
    if not row or not auth_web.verify_password(password, row["password_hash"]):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Неверный email или пароль"},
            status_code=401,
        )

    response = _redirect("/cabinet")
    auth_web.set_cookie(response, row["id"])
    return response


@router.post("/logout")
async def logout():
    response = _redirect("/login")
    auth_web.clear_cookie(response)
    return response


# ── protected cabinet ─────────────────────────────────────────────────────────

@router.get("/cabinet", response_class=HTMLResponse)
async def cabinet(request: Request, user=Depends(auth_web.get_current_user)):
    db = request.app.state.db

    # Server health
    try:
        await db.fetchval("SELECT 1")
        server_status = "online"
    except Exception:
        server_status = "degraded"

    # Bootstrap URL
    bootstrap_url = None
    try:
        profile = await _ensure_bootstrap_profile(db)
        if profile:
            bootstrap_url = "https://api.seamlean.com/api/v1/bootstrap/active"
    except Exception:
        pass

    # Agents with layer stats
    now_ms = int(time.time() * 1000)
    rows = await db.fetch("""
        SELECT DISTINCT ON (machine_id)
            machine_id,
            (payload->>'agent_version')   AS agent_version,
            (payload->>'layer_stats')     AS layer_stats_json,
            (payload->>'drift_ms')::INT   AS drift_ms,
            timestamp_utc
        FROM events
        WHERE event_type = 'HeartbeatPulse'
        ORDER BY machine_id, timestamp_utc DESC
    """)

    agents = []
    for r in rows:
        lag_sec = (now_ms - r["timestamp_utc"]) // 1000
        if lag_sec < 120:
            agent_status = "online"
        elif lag_sec < 900:
            agent_status = "warning"
        else:
            agent_status = "offline"

        layers = _layer_stats(r["layer_stats_json"])
        agents.append({
            "machine_id":    r["machine_id"][:12],
            "agent_version": r["agent_version"] or "—",
            "status":        agent_status,
            "lag_sec":       lag_sec,
            "drift_ms":      r["drift_ms"] or 0,
            "layers":        layers,
        })

    return templates.TemplateResponse("cabinet.html", {
        "request":       request,
        "user":          dict(user),
        "server_status": server_status,
        "bootstrap_url": bootstrap_url,
        "agents":        agents,
    })
