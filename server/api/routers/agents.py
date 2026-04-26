import time
from fastapi import APIRouter, Depends, Request, HTTPException, status
from pydantic import BaseModel

from auth import require_api_key

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])


class MachineSettingsPatch(BaseModel):
    auto_update: bool | None = None

_STATUS_ONLINE  = "online"   # heartbeat < 2 min ago
_STATUS_WARNING = "warning"  # heartbeat 2–15 min ago
_STATUS_OFFLINE = "offline"  # heartbeat > 15 min ago


@router.get("/agents")
async def list_agents(request: Request):
    now_ms       = int(time.time() * 1000)
    one_hour_ago = now_ms - 3_600_000
    one_day_ago  = now_ms - 86_400_000

    rows = await request.app.state.db.fetch("""
        SELECT DISTINCT ON (e.machine_id)
            e.machine_id,
            (e.payload->>'user_id')                                                AS user_id,
            (e.payload->>'payload')::jsonb->>'AgentVersion'                        AS agent_version,
            (e.payload->>'payload')::jsonb->>'Hostname'                            AS hostname,
            (e.payload->>'payload')::jsonb->>'Username'                            AS username,
            (e.payload->>'payload')::jsonb->>'Domain'                              AS domain,
            (e.payload->>'payload')::jsonb->>'LanIp'                               AS lan_ip,
            w.wan_ip                                                               AS wan_ip,
            (e.payload->>'drift_ms')::INT                                          AS drift_ms,
            ((e.payload->>'payload')::jsonb->'LayerStats')::text                   AS layer_stats,
            e.timestamp_utc
        FROM events e
        LEFT JOIN machine_wan_ips w ON w.machine_id = e.machine_id
        WHERE e.event_type = 'HeartbeatPulse'
        ORDER BY e.machine_id, e.timestamp_utc DESC
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

    # Estimate data_mb per machine using event counts + avg row size from pg stats.
    # Avoids a full-table SUM(octet_length(...)) scan that causes 5s timeout in cloud _api().
    data_rows = await request.app.state.db.fetch("""
        WITH machine_counts AS (
            SELECT machine_id, COUNT(*) AS cnt
            FROM events
            GROUP BY machine_id
        ),
        table_stats AS (
            SELECT
                GREATEST(reltuples::BIGINT, 1) AS estimated_rows,
                pg_total_relation_size('events') AS total_bytes
            FROM pg_class
            WHERE relname = 'events'
        )
        SELECT mc.machine_id,
               ROUND((mc.cnt * ts.total_bytes / NULLIF(ts.estimated_rows, 0) / 1048576.0)::NUMERIC, 1) AS data_mb
        FROM machine_counts mc, table_stats ts
    """)

    data_index: dict[str, float] = {r["machine_id"]: float(r["data_mb"]) for r in data_rows}

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
            "username":      r["username"],
            "domain":        r["domain"],
            "lan_ip":        r["lan_ip"],
            "wan_ip":        r["wan_ip"],
            "agent_version": r["agent_version"],
            "status":        status,
            "lag_sec":       lag_sec,
            "drift_ms":      r["drift_ms"],
            "last_seen_ts":  r["timestamp_utc"],
            "layer_stats":   r["layer_stats"],
            "layer_counts":  layer_index.get(r["machine_id"], {}),
            "data_mb":       data_index.get(r["machine_id"]),
        })

    return {"agents": agents, "count": len(agents)}


@router.get("/machines/{machine_id}/settings")
async def get_machine_settings(machine_id: str, request: Request):
    row = await request.app.state.db.fetchrow(
        "SELECT machine_id, auto_update FROM machine_settings WHERE machine_id = $1", machine_id
    )
    if row:
        return {"machine_id": machine_id, "auto_update": row["auto_update"]}
    return {"machine_id": machine_id, "auto_update": True}


@router.patch("/machines/{machine_id}/settings")
async def patch_machine_settings(machine_id: str, body: MachineSettingsPatch, request: Request):
    if body.auto_update is None:
        raise HTTPException(status_code=422, detail="No fields to update")
    await request.app.state.db.execute(
        """INSERT INTO machine_settings (machine_id, auto_update) VALUES ($1, $2)
           ON CONFLICT (machine_id) DO UPDATE SET auto_update = EXCLUDED.auto_update""",
        machine_id, body.auto_update,
    )
    return {"machine_id": machine_id, "auto_update": body.auto_update}


@router.post("/machines/{machine_id}/force-update")
async def force_update_machine(machine_id: str, request: Request):
    await request.app.state.db.execute(
        """INSERT INTO commands (machine_id, command, issued_by)
           VALUES ($1, 'force_update', 'cloud')""",
        machine_id,
    )
    return {"ok": True, "machine_id": machine_id, "command": "force_update"}
