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
from routers import events, errors, commands, screenshots, updates, agents, etl, bootstrap, admin, extension

_SERVER_NAME = os.environ.get("SERVER_NAME", "server")

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


# ── Admin telemetry helpers ───────────────────────────────────────────────────

async def _get_watermark(pool) -> int:
    """Return last event id already forwarded to cloud-admin."""
    row = await pool.fetchrow(
        "SELECT value FROM admin_state WHERE key='last_forwarded_error_id'"
    )
    return int(row["value"]) if row else 0


async def _set_watermark(pool, event_id: int) -> None:
    await pool.execute(
        """INSERT INTO admin_state (key, value, updated_at)
           VALUES ('last_forwarded_error_id', $1, NOW())
           ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()""",
        str(event_id),
    )


async def _fetch_pending_errors(pool, limit: int = 100) -> list[dict]:
    """Fetch LayerError events not yet forwarded to cloud-admin."""
    watermark = await _get_watermark(pool)
    rows = await pool.fetch(
        """SELECT id, machine_id, event_type, layer, window_title,
                  element_type, element_name, raw_message,
                  loaded_at, payload
           FROM events
           WHERE event_type = 'LayerError' AND id > $1
           ORDER BY id ASC
           LIMIT $2""",
        watermark, limit,
    )
    return [
        {
            "id": r["id"],
            "machine_id": r["machine_id"],
            "layer": r["layer"],
            "window_title": r["window_title"],
            "element_type": r["element_type"],
            "element_name": r["element_name"],
            "raw_message": r["raw_message"],
            "loaded_at": r["loaded_at"].isoformat() if r["loaded_at"] else None,
            "payload": (r["payload"] if isinstance(r["payload"], dict)
                        else (json.loads(r["payload"]) if r["payload"] else {})),
        }
        for r in rows
    ]


def _get_docker_errors(container: str, since_minutes: int = 10) -> list[str]:
    """Read error lines from a container's logs via docker.sock (no extra deps)."""
    import http.client as _hc
    import socket as _sock
    import time as _time

    sock_path = "/var/run/docker.sock"
    if not os.path.exists(sock_path):
        return []

    class _UnixHTTP(_hc.HTTPConnection):
        def connect(self):
            self.sock = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
            self.sock.settimeout(5)
            self.sock.connect(sock_path)

    try:
        since_ts = int(_time.time()) - since_minutes * 60
        path = f"/containers/{container}/logs?stdout=1&stderr=1&since={since_ts}&tail=500"
        conn = _UnixHTTP("localhost")
        conn.request("GET", path, headers={"Host": "localhost"})
        resp = conn.getresponse()
        if resp.status != 200:
            return []
        raw = resp.read()
        # Strip 8-byte docker log frame headers
        lines, i = [], 0
        while i + 8 <= len(raw):
            size = int.from_bytes(raw[i + 4:i + 8], "big")
            i += 8
            chunk = raw[i:i + size].decode("utf-8", errors="replace")
            i += size
            lines.extend(chunk.splitlines())
        if not lines:
            lines = raw.decode("utf-8", errors="replace").splitlines()
        keywords = ("error", "exception", "traceback", "critical", "fatal")
        return [ln for ln in lines if any(kw in ln.lower() for kw in keywords)][:200]
    except Exception as exc:
        logger.debug("_get_docker_errors %s: %s", container, exc)
        return []


async def _push_errors_to_cloud(pool) -> None:
    """Collect pending LayerErrors + docker logs and POST to cloud-admin/ingest."""
    if not _CLOUD_URL or not _CLOUD_SERVER_TOKEN:
        return
    try:
        errors_data = await _fetch_pending_errors(pool)
        docker_errors = await asyncio.to_thread(
            _get_docker_errors, f"{_SERVER_NAME}_api", 10
        )
        if not errors_data and not docker_errors:
            return

        payload = {
            "type": "errors",
            "errors": errors_data,
            "docker_errors": docker_errors,
        }
        await asyncio.to_thread(
            _http_post_json,
            f"{_CLOUD_URL}/api/v1/cloud/heartbeat",
            payload,
            _CLOUD_SERVER_TOKEN,
        )
        if errors_data:
            await _set_watermark(pool, errors_data[-1]["id"])
        logger.info(
            "pushed %d layer_errors + %d docker_errors to cloud",
            len(errors_data), len(docker_errors),
        )
    except Exception as exc:
        logger.debug("_push_errors_to_cloud: %s", exc)


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


async def _heartbeat_loop(pool):
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


async def _error_push_loop(pool):
    """Event-driven push: LISTEN on 'layer_error' PostgreSQL channel.

    On each NOTIFY (fired by trg_layer_error_notify trigger on INSERT),
    collect all pending LayerErrors + recent docker error logs and POST
    to cloud-admin/ingest immediately.  Falls back to 60-second polling
    in case a NOTIFY is lost (network blip, pool reconnect, etc.).
    """
    if not _CLOUD_URL or not _CLOUD_SERVER_TOKEN:
        return

    import asyncpg as _asyncpg

    notify_event = asyncio.Event()

    def _on_notify(conn, pid, channel, payload):
        notify_event.set()

    conn = None
    while True:
        try:
            conn = await _asyncpg.connect(os.environ["POSTGRES_DSN"])
            await conn.add_listener("layer_error", _on_notify)
            logger.info("_error_push_loop: listening on 'layer_error' channel")

            while True:
                try:
                    await asyncio.wait_for(notify_event.wait(), timeout=60.0)
                except asyncio.TimeoutError:
                    pass  # 60s fallback poll
                notify_event.clear()
                await _push_errors_to_cloud(pool)

        except Exception as exc:
            logger.warning("_error_push_loop reconnecting: %s", exc)
            await asyncio.sleep(15)
        finally:
            if conn and not conn.is_closed():
                try:
                    await conn.remove_listener("layer_error", _on_notify)
                    await conn.close()
                except Exception:
                    pass
            conn = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await db.create_pool()
    app.state.event_queue = db.EventQueue(app.state.db)
    app.state.event_queue.start()
    await storage.ensure_bucket()
    await _ensure_bootstrap(app.state.db)
    async with app.state.db.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS machine_wan_ips (
                machine_id  TEXT PRIMARY KEY,
                wan_ip      TEXT NOT NULL,
                updated_at  BIGINT NOT NULL
            )
        """)
    catchup_task    = asyncio.create_task(_catchup_loop())
    heartbeat_task  = asyncio.create_task(_heartbeat_loop(app.state.db))
    error_push_task = asyncio.create_task(_error_push_loop(app.state.db))
    yield
    catchup_task.cancel()
    heartbeat_task.cancel()
    error_push_task.cancel()
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
app.include_router(admin.router)
app.include_router(extension.router)


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
