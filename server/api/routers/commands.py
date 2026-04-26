import json
from fastapi import APIRouter, Request, HTTPException, status, Depends
from auth import require_agent_key
from models import CommandAck

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_agent_key)])


@router.get("/commands/{machine_id}")
async def get_command(machine_id: str, request: Request):
    pool = request.app.state.db
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT command_id, command, params, issued_at
               FROM commands
               WHERE machine_id = $1 AND status = 'pending'
               ORDER BY issued_at
               LIMIT 1""",
            machine_id,
        )
    if not row:
        return None  # 200 null — agent polls every 60s, no command is normal

    return {
        "command_id": str(row["command_id"]),
        "command": row["command"],
        "params": row["params"] or {},
        "issued_at": row["issued_at"].isoformat(),
    }


@router.post("/commands/{machine_id}/ack", status_code=200)
async def ack_command(machine_id: str, ack: CommandAck, request: Request):
    pool = request.app.state.db
    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE commands
               SET status = $1, message = $2, acked_at = NOW()
               WHERE command_id = $3 AND machine_id = $4""",
            ack.status, ack.message, str(ack.command_id), machine_id,
        )
    if result == "UPDATE 0":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Command not found")
    return {"status": "ok"}
