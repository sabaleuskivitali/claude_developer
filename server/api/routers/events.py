import time
from fastapi import APIRouter, Request, HTTPException, status
from models import EventsBatch

router = APIRouter(prefix="/api/v1")


def _extract_wan_ip(request: Request) -> str | None:
    cf = request.headers.get("CF-Connecting-IP")
    if cf:
        return cf.strip()
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return None


@router.post("/events")
async def post_events(batch: EventsBatch, request: Request):
    queue     = request.app.state.event_queue
    server_ts = int(time.time() * 1000)

    failed = 0
    for event in batch.events:
        try:
            await queue.put(event)
        except RuntimeError:
            failed += 1

    if batch.events and failed == len(batch.events):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Queue full")

    # Capture WAN IP from CF/proxy headers — upsert per machine_id
    wan_ip = _extract_wan_ip(request)
    if wan_ip and batch.events:
        machine_ids = {e.machine_id for e in batch.events}
        now_ms = server_ts
        try:
            async with request.app.state.db.acquire() as conn:
                await conn.executemany(
                    """INSERT INTO machine_wan_ips (machine_id, wan_ip, updated_at)
                       VALUES ($1, $2, $3)
                       ON CONFLICT (machine_id) DO UPDATE
                       SET wan_ip = EXCLUDED.wan_ip, updated_at = EXCLUDED.updated_at""",
                    [(mid, wan_ip, now_ms) for mid in machine_ids],
                )
        except Exception:
            pass  # non-critical, never block event ingestion

    return {
        "accepted": len(batch.events) - failed,
        "rejected": failed,
        "server_ts": server_ts,
        "received_client_ts": batch.client_ts,
    }
