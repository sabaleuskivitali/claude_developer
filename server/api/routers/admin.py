"""Admin router — called by cloud-admin to execute fixes on the server.

Auth: X-Server-Token must match CLOUD_SERVER_TOKEN env var (same token
the server uses to send heartbeats to cloud — no separate secret needed).

Actions:
  restart_container   — docker restart <name>
  get_logs            — docker logs (last N lines, last M minutes)
  list_containers     — list running containers
  get_metrics         — DB stats + agent counts
  send_agent_command  — INSERT into commands table (picked up by agent's CommandPoller)
"""

import asyncio
import http.client
import json
import logging
import os
import socket
import time
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin")

_CLOUD_SERVER_TOKEN = os.environ.get("CLOUD_SERVER_TOKEN", "")
_DOCKER_SOCK = "/var/run/docker.sock"


# ── Auth ──────────────────────────────────────────────────────────────────────

def _require_cloud_token(request: Request) -> None:
    token = request.headers.get("X-Server-Token", "")
    if not token or not _CLOUD_SERVER_TOKEN or token != _CLOUD_SERVER_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")


# ── Docker socket client (stdlib only, no extra deps) ─────────────────────────

class _UnixHTTP(http.client.HTTPConnection):
    """HTTPConnection that talks to /var/run/docker.sock."""
    def __init__(self):
        super().__init__("localhost")

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(_DOCKER_SOCK)


def _docker_available() -> bool:
    return os.path.exists(_DOCKER_SOCK)


def _docker_request(method: str, path: str) -> tuple[int, bytes]:
    conn = _UnixHTTP()
    conn.request(method, path, headers={"Host": "localhost", "Content-Length": "0"})
    resp = conn.getresponse()
    return resp.status, resp.read()


def _parse_log_stream(raw: bytes) -> str:
    """Docker multiplexed log stream: each frame has an 8-byte header."""
    out, i = [], 0
    while i + 8 <= len(raw):
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        i += 8
        chunk = raw[i:i + size]
        out.append(chunk.decode("utf-8", errors="replace"))
        i += size
    # Fallback: if no frames parsed, treat as raw text
    return "".join(out) if out else raw.decode("utf-8", errors="replace")


def _docker_logs(container: str, since_minutes: int = 60, tail: int = 200) -> str:
    if not _docker_available():
        return ""
    since_ts = int(time.time()) - since_minutes * 60
    path = f"/containers/{container}/logs?stdout=1&stderr=1&since={since_ts}&tail={tail}"
    status, raw = _docker_request("GET", path)
    if status != 200:
        logger.warning("docker logs %s → HTTP %s", container, status)
        return ""
    return _parse_log_stream(raw)


def _docker_list_containers() -> list[dict]:
    if not _docker_available():
        return []
    status, raw = _docker_request("GET", "/containers/json?all=1")
    if status != 200:
        return []
    return json.loads(raw)


def _docker_restart(container: str) -> bool:
    if not _docker_available():
        return False
    status, _ = _docker_request("POST", f"/containers/{container}/restart?t=10")
    return status in (200, 204)


# ── Models ────────────────────────────────────────────────────────────────────

class AdminExecuteIn(BaseModel):
    action: str
    params: dict = {}


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/execute")
async def admin_execute(request: Request, body: AdminExecuteIn):
    """Execute an admin action requested by cloud-admin."""
    _require_cloud_token(request)
    pool = request.app.state.db

    action = body.action

    if action == "restart_container":
        name = body.params.get("container", "").strip()
        if not name:
            raise HTTPException(400, "missing 'container'")
        ok = await asyncio.to_thread(_docker_restart, name)
        logger.info("admin restart_container %s -> %s", name, "ok" if ok else "fail")
        return {"ok": ok, "container": name}

    elif action == "get_logs":
        name = body.params.get("container", "").strip()
        since = int(body.params.get("since_minutes", 60))
        tail = int(body.params.get("tail", 200))
        if not name:
            raise HTTPException(400, "missing 'container'")
        logs = await asyncio.to_thread(_docker_logs, name, since, tail)
        error_lines = [
            ln for ln in logs.splitlines()
            if any(kw in ln.lower() for kw in ("error", "exception", "traceback", "critical", "fatal"))
        ]
        return {"ok": True, "container": name, "lines": len(error_lines), "logs": "\n".join(error_lines)}

    elif action == "list_containers":
        containers = await asyncio.to_thread(_docker_list_containers)
        return {"ok": True, "containers": [
            {"id": c["Id"][:12], "names": c["Names"], "status": c["Status"], "state": c["State"]}
            for c in containers
        ]}

    elif action == "get_metrics":
        async with pool.acquire() as conn:
            db_size = await conn.fetchval("SELECT pg_database_size(current_database())")
            errors_24h = await conn.fetchval(
                "SELECT COUNT(*) FROM events WHERE event_type='LayerError' "
                "AND loaded_at > NOW() - INTERVAL '24 hours'"
            )
            online_agents = await conn.fetchval(
                "SELECT COUNT(DISTINCT machine_id) FROM events "
                "WHERE event_type='HeartbeatPulse' AND loaded_at > NOW() - INTERVAL '2 minutes'"
            )
            pending_events = await conn.fetchval(
                "SELECT COUNT(*) FROM events WHERE vision_done=FALSE AND layer='visual'"
            )
        return {
            "ok": True,
            "db_size_mb": round(db_size / 1024 / 1024, 1),
            "layer_errors_24h": errors_24h,
            "online_agents": online_agents,
            "vision_backlog": pending_events,
        }

    elif action == "send_agent_command":
        machine_id = body.params.get("machine_id", "").strip()
        command = body.params.get("command", "").strip()
        params = body.params.get("params", {})
        if not machine_id or not command:
            raise HTTPException(400, "missing 'machine_id' or 'command'")
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO commands (machine_id, command, params, issued_by)
                   VALUES ($1, $2, $3::jsonb, 'cloud-admin')
                   RETURNING command_id""",
                machine_id, command, json.dumps(params),
            )
        logger.info("admin send_agent_command %s -> %s", machine_id, command)
        return {"ok": True, "command_id": str(row["command_id"])}

    else:
        raise HTTPException(400, f"unknown action: {action!r}")
