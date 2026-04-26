import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, Response

router = APIRouter(prefix="/extension")

_EXTENSION_ID      = os.environ.get("EXTENSION_ID", "")
_EXTENSION_VERSION = os.environ.get("EXTENSION_VERSION", "1.0.0")
_CRX_PATH          = Path(os.environ.get("EXTENSION_CRX_PATH", "/app/extension/extension.crx"))
_SERVER_URL        = os.environ.get("SERVER_URL", "")


@router.get("/update.xml", include_in_schema=False)
async def extension_update_xml():
    """Chrome Update XML — Chrome polls this URL from ExtensionInstallForcelist.
    No auth: Chrome fetches this internally without custom headers."""
    if not _EXTENSION_ID:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Extension not configured")

    crx_url = f"{_SERVER_URL}/extension/extension.crx"
    xml = f"""<?xml version='1.0' encoding='UTF-8'?>
<gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>
  <app appid='{_EXTENSION_ID}'>
    <updatecheck codebase='{crx_url}' version='{_EXTENSION_VERSION}' />
  </app>
</gupdate>"""
    return Response(content=xml, media_type="application/xml")


@router.get("/extension.crx", include_in_schema=False)
async def extension_crx():
    """Serve the packed extension CRX.
    No auth: Chrome fetches this as part of its internal update mechanism."""
    if not _CRX_PATH.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Extension package not found")
    return FileResponse(
        _CRX_PATH,
        media_type="application/x-chrome-extension",
        filename="extension.crx",
    )
