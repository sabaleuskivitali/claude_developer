import os
import json
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from auth import require_agent_key

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_agent_key)])

_PACKAGES_DIR = Path(os.environ.get("UPDATE_PACKAGES_DIR", "/updates"))


@router.get("/updates/latest")
async def get_latest():
    latest = _PACKAGES_DIR / "latest.json"
    if not latest.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No release yet")
    return json.loads(latest.read_text())


@router.get("/updates/{version}/package")
async def get_package(version: str):
    package = _PACKAGES_DIR / version / "WinDiagSvc.zip"
    if not package.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found")
    return FileResponse(package, media_type="application/zip", filename="WinDiagSvc.zip")
