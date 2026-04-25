import time
from fastapi import APIRouter, Depends, Request

from auth import require_api_key

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])

_STATUS_ONLINE  = "online"   # heartbeat < 2 min ago
_STATUS_WARNING = "warning"  # heartbeat 2–15 min ago
_STATUS_OFFLINE = "offline"  # heartbeat > 15 min ago


@router.get("/agents")
async def list_agents(request: Request):
    now_ms       = int(time.time() * 1000)
    one_hour_ago = now_ms - 3_600_000
    one_day_ago  = now_ms - 86_400_000

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

    layer_rows = await request.app.state.db.fetch("""
        SELECT
            machine_id,
            layer,
            COUNT(*) FILTER (WHERE timestamp_utc >= $1 AND event_type != 'LayerError') AS events_1h,
            COUNT(*) FILTER (WHERE timestamp_utc >= $2 AND event_type != 'LayerError') AS events_24h,
            COUNT(*) FILTER (WHERE event_type != 'LayerError')                          AS events_total,
            COUNT(*) FILTER (WHERE timestamp_utc >= $1 AND event_type = 'LayerError')  AS errors_1h,
            COUNT(*) FILTER (WHERE timestamp_utc >= $2 AND event_type = 'LayerError')  AS errors_24h
        FROM events
        WHERE timestamp_utc >= $2
          AND event_type NOT IN ('HeartbeatPulse', 'SyncCompleted')
          AND layer IS NOT NULL
        GROUP BY machine_id, layer
    """, one_hour_ago, one_day_ago)

    layer_index: dict[str, dict] = {}
    for lr in layer_rows:
        mid = lr["machine_id"]
        if mid not in layer_index:
            layer_index[mid] = {}
        layer_index[mid][lr["layer"]] = {
            "events_1h":    int(lr["events_1h"]),
            "events_24h":   int(lr["events_24h"]),
            "events_total": int(lr["events_total"]),
            "errors_1h":    int(lr["errors_1h"]),
            "errors_24h":   int(lr["errors_24h"]),
        }

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
            "layer_counts":  layer_index.get(r["machine_id"], {}),
        })

    return {"agents": agents, "count": len(agents)}
