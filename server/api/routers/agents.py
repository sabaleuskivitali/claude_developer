import time
from fastapi import APIRouter, Depends, Request

from auth import require_api_key

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])

_STATUS_ONLINE  = "online"   # heartbeat < 2 min ago
_STATUS_WARNING = "warning"  # heartbeat 2–15 min ago
_STATUS_OFFLINE = "offline"  # heartbeat > 15 min ago


@router.get("/agents")
async def list_agents(request: Request):
    now_ms = int(time.time() * 1000)

    rows = await request.app.state.db.fetch("""
        SELECT DISTINCT ON (machine_id)
            machine_id,
            (payload->>'user_id')        AS user_id,
            (payload->>'agent_version')  AS agent_version,
            (payload->>'events_buffered')::BIGINT AS events_buffered,
            (payload->>'drift_ms')::INT  AS drift_ms,
            (payload->>'layer_stats')    AS layer_stats,
            timestamp_utc
        FROM events
        WHERE event_type = 'HeartbeatPulse'
        ORDER BY machine_id, timestamp_utc DESC
    """)

    agents = []
    for r in rows:
        lag_ms  = now_ms - r["timestamp_utc"]
        lag_sec = lag_ms // 1000

        if lag_sec < 120:
            status = _STATUS_ONLINE
        elif lag_sec < 900:
            status = _STATUS_WARNING
        else:
            status = _STATUS_OFFLINE

        agents.append({
            "machine_id":      r["machine_id"],
            "user_id":         r["user_id"],
            "agent_version":   r["agent_version"],
            "status":          status,
            "lag_sec":         lag_sec,
            "events_buffered": r["events_buffered"],
            "drift_ms":        r["drift_ms"],
            "last_seen_ts":    r["timestamp_utc"],
        })

    return {"agents": agents, "count": len(agents)}
