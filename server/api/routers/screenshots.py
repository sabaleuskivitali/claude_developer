from fastapi import APIRouter, Request, HTTPException, status, Depends
from auth import require_agent_key
import storage

router = APIRouter(prefix="/api/v1")


@router.put("/screenshots/{machine_id}/{date_str}/{event_id}", status_code=202)
async def upload_screenshot(
    machine_id: str,
    date_str: str,
    event_id: str,
    request: Request,
    _key: str = Depends(require_agent_key),
):
    data = await request.body()
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty body")
    if len(data) > 5 * 1024 * 1024:  # 5 MB max
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="File too large")

    key = storage.screenshot_key(machine_id, date_str, event_id)
    await storage.put_screenshot(key, data)
    return {"path": key}
