import time
from fastapi import APIRouter, Depends, Request, HTTPException, status
from auth import require_api_key
from models import EventsBatch

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])


@router.post("/events")
async def post_events(batch: EventsBatch, request: Request):
    queue = request.app.state.event_queue
    server_ts = int(time.time() * 1000)

    failed = 0
    for event in batch.events:
        try:
            await queue.put(event)
        except RuntimeError:
            failed += 1

    if failed == len(batch.events):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Queue full")

    return {
        "accepted": len(batch.events) - failed,
        "rejected": failed,
        "server_ts": server_ts,
        "received_client_ts": batch.client_ts,
    }
