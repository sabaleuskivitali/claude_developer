"""
Browser extension delivery endpoints.

GET  /extension/update.xml       — Chrome/Edge ExtensionInstallForcelist update manifest
GET  /extension/extension.crx    — packed extension download
POST /extension/upload           — agent installer uploads CRX during --install
                                   auth: enrollment token OR admin API key
"""
import os
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import FileResponse, Response

from auth import require_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/extension")

_EXT_DIR = Path("/app/extension")
_SERVER_URL = os.environ.get("SERVER_URL", "").rstrip("/")

# Env-var fallbacks (manual setup path); file-based values take precedence
_ENV_EXT_ID      = os.environ.get("EXTENSION_ID", "")
_ENV_EXT_VERSION = os.environ.get("EXTENSION_VERSION", "1.0.0")


def _extension_id() -> str:
    p = _EXT_DIR / "extension-id.txt"
    if p.exists():
        return p.read_text().strip()
    return _ENV_EXT_ID


def _extension_version() -> str:
    p = _EXT_DIR / "extension-version.txt"
    if p.exists():
        return p.read_text().strip()
    return _ENV_EXT_VERSION


# ---------------------------------------------------------------------------

@router.get("/update.xml", include_in_schema=False)
async def extension_update_xml():
    ext_id = _extension_id()
    if not ext_id:
        raise HTTPException(404, "Extension not configured")
    crx_url = f"{_SERVER_URL}/extension/extension.crx"
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>"
        f"<app appid='{ext_id}'>"
        f"<updatecheck codebase='{crx_url}' version='{_extension_version()}' />"
        "</app>"
        "</gupdate>"
    )
    return Response(content=xml, media_type="application/xml")


@router.get("/extension.crx", include_in_schema=False)
async def extension_crx():
    crx_path = _EXT_DIR / "extension.crx"
    if not crx_path.exists():
        raise HTTPException(404, "Extension package not available")
    return FileResponse(str(crx_path), media_type="application/x-chrome-extension")


# ---------------------------------------------------------------------------

async def _validate_token(request: Request, token: str) -> bool:
    """Accept a valid (non-expired) enrollment token from the DB."""
    if not token:
        return False
    try:
        row = await request.app.state.db.fetchrow(
            """
            SELECT 1 FROM enrollment_tokens et
             JOIN bootstrap_profiles bp ON bp.id = et.profile_id
            WHERE et.token = $1
              AND et.expires_at > $2
              AND bp.status = 'published'
            """,
            token,
            datetime.now(timezone.utc),
        )
        return row is not None
    except Exception as e:
        logger.warning("Token validation error: %s", e)
        return False


@router.post("/upload", status_code=200)
async def upload_extension(
    request: Request,
    ext_id: str = Query(..., description="Chrome extension ID"),
    version: str = Query("1.0.0"),
):
    """
    Agent installer calls this to push extension.crx to the server.
    Auth: X-Enrollment-Token (enrollment token from bootstrap profile)
       OR X-Api-Key (admin API key).
    Body: raw CRX binary.
    """
    # Auth: admin key or valid enrollment token
    admin_key = os.environ.get("API_KEY", "")
    api_key   = request.headers.get("x-api-key", "")
    enroll_tk = request.headers.get("x-enrollment-token", "")

    authed = (api_key and api_key == admin_key) or await _validate_token(request, enroll_tk)
    if not authed:
        raise HTTPException(401, "Enrollment token or API key required")

    data = await request.body()
    if not data:
        raise HTTPException(400, "Empty body")

    # Minimal CRX/ZIP magic-byte check (PK or Cr24)
    if not (data[:2] == b"PK" or data[:4] == b"Cr24"):
        raise HTTPException(400, "Not a valid CRX/ZIP file")

    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, "File too large (max 20 MB)")

    _EXT_DIR.mkdir(parents=True, exist_ok=True)
    (_EXT_DIR / "extension.crx").write_bytes(data)
    (_EXT_DIR / "extension-id.txt").write_text(ext_id.strip())
    (_EXT_DIR / "extension-version.txt").write_text(version.strip())

    logger.info("Extension uploaded: id=%s version=%s size=%d", ext_id, version, len(data))
    return {"ok": True, "ext_id": ext_id, "version": version, "size": len(data)}
