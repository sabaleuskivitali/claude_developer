import time
import uuid
from fastapi import APIRouter, Request
from models import HeartbeatIn, EventIn

router = APIRouter(prefix="/api/v1")


@router.post("/heartbeat")
async def post_heartbeat(hb: HeartbeatIn, request: Request):
    server_ts = int(time.time() * 1000)
    offset_ms = server_ts - (hb.client_ts + 0)  # simplified; RTT unknown server-side

    event = EventIn(
        event_id=uuid.uuid4(),
        session_id=hb.session_id,
        machine_id=hb.machine_id,
        user_id=hb.user_id,
        timestamp_utc=hb.client_ts,
        synced_ts=hb.client_ts,
        drift_ms=hb.drift_ms,
        drift_rate_ppm=hb.drift_rate_ppm,
        sequence_idx=-1,
        layer="agent",
        event_type="HeartbeatPulse",
        payload=hb.model_dump(mode="json"),
    )
    await request.app.state.event_queue.put(event)

    return {
        "server_ts": server_ts,
        "received_client_ts": hb.client_ts,
        "offset_ms": offset_ms,
    }
