import json
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Request
from auth import require_api_key
from models import ErrorIn

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])


@router.post("/errors", status_code=202)
async def post_error(error: ErrorIn, request: Request):
    pool = request.app.state.db
    try:
        ts = datetime.fromisoformat(error.ts.replace("Z", "+00:00"))
    except ValueError:
        ts = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO install_errors
               (machine_id, stage, error, os_version, agent_version, ts, payload)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            error.machine_id, error.stage, error.error,
            error.os_version, error.agent_version, ts,
            json.dumps(error.payload) if error.payload else None,
        )
    return {"status": "accepted"}
