from fastapi import APIRouter, Depends, Request, HTTPException, status
from fastapi import UploadFile, File
from auth import require_api_key
import storage

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])


@router.put("/screenshots/{machine_id}/{date_str}/{event_id}", status_code=202)
async def upload_screenshot(
    machine_id: str,
    date_str: str,
    event_id: str,
    request: Request,
    file: UploadFile = File(...),
):
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:  # 5 MB max
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="File too large")

    key = storage.screenshot_key(machine_id, date_str, event_id)
    await storage.put_screenshot(key, data)
    return {"path": key}
