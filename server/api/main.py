import os
import shutil
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
import db, storage
from routers import events, errors, heartbeat, commands, screenshots, updates, agents


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await db.create_pool()
    app.state.event_queue = db.EventQueue(app.state.db)
    app.state.event_queue.start()
    await storage.ensure_bucket()
    yield
    await app.state.event_queue.stop()
    await app.state.db.close()


app = FastAPI(title="WinDiag API", version="2.0.0", lifespan=lifespan)

app.include_router(events.router)
app.include_router(errors.router)
app.include_router(heartbeat.router)
app.include_router(commands.router)
app.include_router(screenshots.router)
app.include_router(updates.router)
app.include_router(agents.router)


@app.get("/health")
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

    # Buffered (unsent) events in DB
    try:
        async with request.app.state.db.acquire() as conn:
            buffered = await conn.fetchval(
                "SELECT COUNT(*) FROM events WHERE vision_done = FALSE AND layer = 'visual'"
            )
            old_unsent = await conn.fetchval(
                """SELECT COUNT(*) FROM events
                   WHERE loaded_at < NOW() - INTERVAL '2 hours'
                   AND event_type = 'HeartbeatPulse'"""
            )
        checks["vision_backlog"] = buffered
        if old_unsent and old_unsent > 0:
            checks["stale_agents"] = int(old_unsent)
            status = "degraded"
    except Exception:
        pass

    return {"status": status, **checks}


@app.get("/discovery")
async def discovery():
    """Plain endpoint (no auth). Returns port and TLS thumbprint for cross-subnet agents."""
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
