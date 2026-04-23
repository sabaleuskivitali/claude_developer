import os
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import db, storage
from routers import events, errors, heartbeat, commands, screenshots, updates, agents, etl, bootstrap

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await db.create_pool()
    app.state.event_queue = db.EventQueue(app.state.db)
    app.state.event_queue.start()
    await storage.ensure_bucket()
    yield
    await app.state.event_queue.stop()
    await app.state.db.close()


_VERSION = _read_version()
app = FastAPI(title="WinDiag API", version=_VERSION, lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(events.router)
app.include_router(errors.router)
app.include_router(heartbeat.router)
app.include_router(commands.router)
app.include_router(screenshots.router)
app.include_router(updates.router)
app.include_router(agents.router)
app.include_router(etl.router)
app.include_router(bootstrap.router)


@app.get("/health")
@limiter.limit("10/minute")
async def health(request: Request):
    checks = {}
    status = "ok"

    # DB
    try:
        async with request.app.state.db.acquire() as conn:
            await conn.fetchval("SELECT 1")
        checks["db"] = "ok"
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
        free_gb = round(free / 1024 ** 3, 1)
        checks["disk_free_gb"] = free_gb
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
