import os
from fastapi import Security, HTTPException, status
from fastapi.security import APIKeyHeader

_API_KEY_HEADER = APIKeyHeader(name="X-Api-Key", auto_error=False)


def require_api_key(key: str = Security(_API_KEY_HEADER)) -> str:
    expected = os.environ["API_KEY"]
    if not key or key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return key
