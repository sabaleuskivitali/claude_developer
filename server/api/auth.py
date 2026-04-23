import os
from fastapi import Security, HTTPException, status, Request
from fastapi.security import APIKeyHeader

_API_KEY_HEADER = APIKeyHeader(name="X-Api-Key", auto_error=False)


def require_api_key(key: str = Security(_API_KEY_HEADER)) -> str:
    """Admin key — global env var. Used for admin/internal endpoints."""
    expected = os.environ["API_KEY"]
    if not key or key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return key


async def require_agent_key(request: Request, key: str = Security(_API_KEY_HEADER)) -> str:
    """Accept global admin key OR per-machine enrollment key from agent_api_keys table."""
    if not key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing API key")

    if key == os.environ["API_KEY"]:
        return key

    row = await request.app.state.db.fetchrow(
        "SELECT machine_id FROM agent_api_keys WHERE api_key = $1 AND expires_at > NOW()",
        key,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return key
