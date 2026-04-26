import asyncio
import json
import logging
import os
import random
import shutil
import ssl
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import db, storage

_CLOUD_URL          = os.environ.get("CLOUD_URL", "").rstrip("/")
_CLOUD_SERVER_TOKEN = os.environ.get("CLOUD_SERVER_TOKEN", "")
_API_KEY            = os.environ.get("API_KEY", "")
_SERVER_URL         = os.environ.get("SERVER_URL", "")

_ssl_ctx_server = ssl.create_default_context()
_ssl_ctx_server.check_hostname = False
_ssl_ctx_server.verify_mode = ssl.CERT_NONE

logger = logging.getLogger(__name__)
from routers import events, errors, commands, screenshots, updates, agents, etl, bootstrap, meetings

def _read_version() -> str:
    for p in (Path(__file__).parent / "VERSION", Path("/app/VERSION")):
        if p.exists():
            return p.read_text().strip()
    return os.environ.get("APP_VERSION", "unknown")

# 300/min per IP: handles up to ~75 agents behind one corporate NAT (each agent = ~4 req/min).
# Global 2000/sec: absorbs bursts from thousands of agents without false-positives.
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["300/minute"],
    application_limits=["2000/second"],
)


async def _ensure_bootstrap(pool):
    """Auto-create and publish a default bootstrap profile if none exists."""
    from bootstrap import store as bs_store
    from bootstrap.generator import generate_profile, DeploymentContext
    profiles = await bs_store.list_profiles(pool)
    if profiles:
        return
    server_url = os.environ.get("SERVER_URL", "")
    ctx = DeploymentContext(server_url=server_url, tenant_id="default", site_id="default")
    try:
        signed = generate_profile(ctx)
        profile_id = await bs_store.create(pool, signed, ctx)
        await bs_store.approve(pool, profile_id)
        await bs_store.publish(pool, profile_id)
        token = signed.get_profile().enrollment.token
        expires_at = signed.get_profile().enrollment.expires_at
        from datetime import datetime, timezone
        await pool.execute(
            "INSERT INTO enrollment_tokens (token, profile_id, expires_at) VALUES ($1, $2::UUID, $3)",
            token, profile_id, datetime.fromisoformat(expires_at).astimezone(timezone.utc),
        )
    except Exception as e:
        logger.error("_ensure_bootstrap failed: %s", e)


def _http_get_json(url: str, token: str = "") -> dict:
    headers = {"User-Agent": "Seamlean-Server/1.0"}
    if token:
        headers["X-Server-Token"] = token
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx_server) as r:
        return json.loads(r.read())


def _http_post_json(url: str, body: dict, token: str = "") -> dict:
    headers = {"Content-Type": "application/json", "User-Agent": "Seamlean-Server/1.0"}
    if token:
        headers["X-Server-Token"] = token
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx_server) as r:
        return json.loads(r.read())


def _current_cached_version() -> str | None:
    from pathlib import Path as _Path
    import os as _os
    pkg_dir = _Path(_os.environ.get("UPDATE_PACKAGES_DIR", "/updates"))
    lj = pkg_dir / "latest.json"
    if lj.exists():
        try:
            return json.loads(lj.read_text()).get("version")
        except Exception:
            pass
    return None


async def _catchup_loop():
    if not _CLOUD_URL or not _CLOUD_SERVER_TOKEN:
        return
    await asyncio.sleep(random.randint(0, 60))
    while True:
        try:
            data = await asyncio.to_thread(
                _http_get_json,
                f"{_CLOUD_URL}/api/v1/cloud/latest-agent",
                _CLOUD_SERVER_TOKEN,
            )
            version = data.get("version")
            if version and version != _current_cached_version():
                from routers.updates import _download_and_cache
                asyncio.create_task(
                    _download_and_cache(version, data.get("sha256", ""), data.get("exe_url", ""))
                )
        except Exception as e:
            logger.debug("_catchup_loop: %s", e)
        await asyncio.sleep(86400)


async def _heartbeat_loop():
    if not _CLOUD_URL or not _CLOUD_SERVER_TOKEN:
        return
    version = _read_version()
    while True:
        try:
            await asyncio.to_thread(
                _http_post_json,
                f"{_CLOUD_URL}/api/v1/cloud/heartbeat",
                {"tunnel_url": _SERVER_URL, "api_key": _API_KEY, "version": version},
                _CLOUD_SERVER_TOKEN,
            )
        except Exception as e:
            logger.debug("_heartbeat_loop: %s", e)
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await db.create_pool()
    app.state.event_queue = db.EventQueue(app.state.db)
    app.state.event_queue.start()
    await storage.ensure_bucket()
    await storage.ensure_audio_bucket()
    await _ensure_bootstrap(app.state.db)
    async with app.state.db.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS machine_wan_ips (
                machine_id  TEXT PRIMARY KEY,
                wan_ip      TEXT NOT NULL,
                updated_at  BIGINT NOT NULL
            )
        """)
    catchup_task   = asyncio.create_task(_catchup_loop())
    heartbeat_task = asyncio.create_task(_heartbeat_loop())
    yield
    catchup_task.cancel()
    heartbeat_task.cancel()
    await app.state.event_queue.stop()
    await app.state.db.close()


_VERSION = _read_version()
app = FastAPI(title="WinDiag API", version=_VERSION, lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://seamlean.com"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["X-Api-Key", "Content-Type"],
)


@app.middleware("http")
async def add_noindex(request, call_next):
    response = await call_next(request)
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@app.get("/robots.txt", include_in_schema=False)
async def robots():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("https://seamlean.com", status_code=302)


@app.exception_handler(404)
async def not_found(request: Request, exc):
    # Pass through 404s raised inside API routes (they have meaningful detail)
    if request.url.path.startswith("/api/"):
        from fastapi.responses import JSONResponse as _J
        detail = getattr(exc, "detail", "Not Found")
        return _J(status_code=404, content={"detail": detail})
    return JSONResponse(
        status_code=404,
        content={
            "error": "Not Found",
            "message": "This is the Seamlean agent API (machine use only).",
            "website": "https://seamlean.com",
        },
    )


app.include_router(events.router)
app.include_router(errors.router)
app.include_router(commands.router)
app.include_router(screenshots.router)
app.include_router(updates.router)
app.include_router(agents.router)
app.include_router(etl.router)
app.include_router(bootstrap.router)
app.include_router(meetings.router)


@app.get("/health")
@limiter.limit("10/minute")
async def health(request: Request):
    checks = {}
    status = "ok"

    # DB
    try:
        async with request.app.state.db.acquire() as conn:
            await conn.fetchval("SELECT 1")
            row = await conn.fetchrow("SELECT pg_database_size(current_database()) AS sz")
            checks["db"] = "ok"
            checks["db_size_mb"] = round(row["sz"] / 1024 / 1024, 1)
    except Exception as e:
        checks["db"] = f"error: {e}"
        status = "degraded"

    # MinIO
    try:
        await storage.ensure_bucket()
        checks["minio"] = "ok"
    except Exception as e:
        checks["minio"] = f"error: {e}"
        status = "degraded"

    # Disk space
    try:
        total, used, free = shutil.disk_usage("/")
        free_gb  = round(free  / 1024 ** 3, 1)
        total_gb = round(total / 1024 ** 3, 1)
        checks["disk_free_gb"]  = free_gb
        checks["disk_total_gb"] = total_gb
        if free_gb < 5:
            checks["disk"] = "warning: low"
            status = "degraded"
        else:
            checks["disk"] = "ok"
    except Exception as e:
        checks["disk"] = f"error: {e}"

    # Event queue depth
    try:
        q = request.app.state.event_queue
        checks["queue_depth"] = q._queue.qsize()
    except Exception:
        pass

    # Vision backlog + stale agents (online but not syncing)
    try:
        async with request.app.state.db.acquire() as conn:
            vision_backlog = await conn.fetchval(
                "SELECT COUNT(*) FROM events WHERE vision_done = FALSE AND layer = 'visual'"
            )
            stale = await conn.fetch(
                """SELECT machine_id, MAX((payload->>'events_buffered')::INT) AS buffered
                   FROM events
                   WHERE event_type = 'HeartbeatPulse'
                     AND loaded_at > NOW() - INTERVAL '15 minutes'
                   GROUP BY machine_id
                   HAVING MAX((payload->>'events_buffered')::INT) > 1000"""
            )
        checks["vision_backlog"] = int(vision_backlog)
        if stale:
            checks["stale_agents"] = [
                {"machine_id": r["machine_id"], "buffered": r["buffered"]} for r in stale
            ]
            status = "degraded"
    except Exception:
        pass

    # ETL last run
    try:
        async with request.app.state.db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT run_at, files, rows, duration_ms, error FROM etl_status ORDER BY id DESC LIMIT 1"
            )
        if row:
            checks["etl_last_run"] = {
                "run_at":      row["run_at"].isoformat(),
                "files":       row["files"],
                "rows":        row["rows"],
                "duration_ms": row["duration_ms"],
                "error":       row["error"],
            }
            if row["error"]:
                checks["etl_last_run"]["status"] = "error"
                status = "degraded"
            else:
                checks["etl_last_run"]["status"] = "ok"
    except Exception:
        pass

    return {"status": status, "version": _VERSION, **checks}


@app.get("/discovery")
@limiter.limit("1/second")
@limiter.limit("20/second", key_func=lambda: "global")
async def discovery(request: Request):
    """Plain endpoint (no auth). Returns port for cross-subnet agents."""
    port_env = "/app/runtime/port.env"
    port = int(os.environ.get("PORT", 49200))
    if os.path.exists(port_env):
        for line in open(port_env).readlines():
            if line.startswith("PORT="):
                port = int(line.strip().split("=")[1])

    thumbprint = ""
    thumb_file = "/certs/thumbprint.txt"
    if os.path.exists(thumb_file):
        thumbprint = open(thumb_file).read().strip()

    return {"port": port, "thumbprint": thumbprint, "version": 2}
