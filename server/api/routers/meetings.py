from fastapi import APIRouter, Request, HTTPException, status, UploadFile, File
from pydantic import BaseModel
from typing import Optional
import storage

router = APIRouter(prefix="/api/v1")

_MAX_AUDIO_BYTES = 200 * 1024 * 1024  # 200 MB


class MeetingMeta(BaseModel):
    user_id: str
    started_at: int           # unix ms
    ended_at: int             # unix ms
    process_name: Optional[str] = None
    window_title: Optional[str] = None
    trigger: Optional[str] = None  # "process" | "window_title" | "browser_url"


@router.post("/meetings/{machine_id}/{meeting_id}/meta", status_code=202)
async def upsert_meeting_meta(
    machine_id: str,
    meeting_id: str,
    meta: MeetingMeta,
    request: Request,
):
    duration_sec = (meta.ended_at - meta.started_at) / 1000
    async with request.app.state.db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO meeting_recordings (
                meeting_id, machine_id, user_id,
                started_at, ended_at, duration_sec,
                process_name, window_title, trigger
            ) VALUES ($1::UUID, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (meeting_id) DO UPDATE SET
                ended_at     = EXCLUDED.ended_at,
                duration_sec = EXCLUDED.duration_sec,
                process_name = EXCLUDED.process_name,
                window_title = EXCLUDED.window_title,
                trigger      = EXCLUDED.trigger
            """,
            meeting_id, machine_id, meta.user_id,
            meta.started_at, meta.ended_at, duration_sec,
            meta.process_name, meta.window_title, meta.trigger,
        )
    return {"meeting_id": meeting_id}


@router.post("/meetings/{machine_id}/{meeting_id}/audio/{channel}", status_code=202)
async def upload_audio(
    machine_id: str,
    meeting_id: str,
    channel: str,
    request: Request,
    file: UploadFile = File(...),
):
    if channel not in ("mic", "loopback"):
        raise HTTPException(status_code=400, detail="channel must be 'mic' or 'loopback'")

    data = await file.read()
    if len(data) > _MAX_AUDIO_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="File too large")
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    key = storage.audio_key(machine_id, meeting_id, channel)
    await storage.put_audio(key, data)

    if channel == "mic":
        sql = """
            INSERT INTO meeting_recordings (meeting_id, machine_id, user_id, started_at, mic_path)
            VALUES ($1::UUID, $2, '', 0, $3)
            ON CONFLICT (meeting_id) DO UPDATE SET mic_path = EXCLUDED.mic_path
        """
    else:
        sql = """
            INSERT INTO meeting_recordings (meeting_id, machine_id, user_id, started_at, loopback_path)
            VALUES ($1::UUID, $2, '', 0, $3)
            ON CONFLICT (meeting_id) DO UPDATE SET loopback_path = EXCLUDED.loopback_path
        """
    async with request.app.state.db.acquire() as conn:
        await conn.execute(sql, meeting_id, machine_id, key)
    return {"path": key}
