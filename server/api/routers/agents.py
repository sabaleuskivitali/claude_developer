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
            (payload->>'user_id')                                                AS user_id,
            (payload->>'payload')::jsonb->>'AgentVersion'                        AS agent_version,
            (payload->>'payload')::jsonb->>'Hostname'                            AS hostname,
            (payload->>'drift_ms')::INT                                          AS drift_ms,
            ((payload->>'payload')::jsonb->'LayerStats')::text                   AS layer_stats,
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
            "machine_id":    r["machine_id"],
            "user_id":       r["user_id"],
            "hostname":      r["hostname"],
            "agent_version": r["agent_version"],
            "status":        status,
            "lag_sec":       lag_sec,
            "drift_ms":      r["drift_ms"],
            "last_seen_ts":  r["timestamp_utc"],
            "layer_stats":   r["layer_stats"],
        })

    return {"agents": agents, "count": len(agents)}
