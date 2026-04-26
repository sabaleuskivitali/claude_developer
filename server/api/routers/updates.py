import asyncio
import hashlib
import json
import logging
import os
import ssl
import urllib.request
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from auth import require_agent_key, require_api_key
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")

_PACKAGES_DIR  = Path(os.environ.get("UPDATE_PACKAGES_DIR", "/updates"))
_GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

_lock: asyncio.Lock | None = None
_events: dict[str, asyncio.Event] = {}


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def _latest_json_path() -> Path:
    return _PACKAGES_DIR / "latest.json"


def _is_cached(version: str) -> bool:
    return (_PACKAGES_DIR / version / "Seamlean.Agent.exe").exists()


def _write_latest_json(version: str, sha256: str, exe_url: str):
    _PACKAGES_DIR.mkdir(parents=True, exist_ok=True)
    data = {"version": version, "sha256": sha256, "exe_url": exe_url}
    _latest_json_path().write_text(json.dumps(data))


def _cleanup_old(keep: int = 2):
    dirs = sorted(
        [d for d in _PACKAGES_DIR.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
    )
    for d in dirs[:-keep]:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def _download_sync(version: str, sha256: str, exe_url: str):
    dest_dir = _PACKAGES_DIR / version
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / "Seamlean.Agent.exe"
    headers = {"User-Agent": "Seamlean-Server/1.0"}
    if _GITHUB_TOKEN:
        headers["Authorization"] = f"token {_GITHUB_TOKEN}"
    req = urllib.request.Request(exe_url, headers=headers)
    with urllib.request.urlopen(req, timeout=120, context=_ssl_ctx) as resp:
        data = resp.read()
    actual = hashlib.sha256(data).hexdigest().lower()
    if sha256 and actual != sha256.lower():
        raise ValueError(f"SHA256 mismatch: expected {sha256}, got {actual}")
    dest_file.write_bytes(data)
    logger.info("Downloaded agent %s (%d bytes)", version, len(data))


async def _download_and_cache(version: str, sha256: str, exe_url: str):
    event = _events.setdefault(version, asyncio.Event())
    async with _get_lock():
        if _is_cached(version):
            _write_latest_json(version, sha256, exe_url)
            event.set()
            return
        try:
            await asyncio.to_thread(_download_sync, version, sha256, exe_url)
            _write_latest_json(version, sha256, exe_url)
            _cleanup_old(keep=2)
            event.set()
        except Exception as e:
            logger.error("Download failed for %s: %s", version, e)
            _events.pop(version, None)


@router.get("/updates/latest")
async def get_latest():
    path = _latest_json_path()
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No release yet")
    return json.loads(path.read_text())


@router.get("/updates/{version}/package")
async def get_package(version: str, _key: str = Depends(require_agent_key)):
    event = _events.get(version)
    if event and not event.is_set():
        try:
            await asyncio.wait_for(event.wait(), timeout=120)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=503, detail="Download timed out")

    package = _PACKAGES_DIR / version / "Seamlean.Agent.exe"
    if not package.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found")
    return FileResponse(
        package,
        media_type="application/octet-stream",
        filename="Seamlean.Agent.exe",
    )


@router.post("/updates/notify")
async def notify_update(request: Request, background_tasks: BackgroundTasks, _key: str = Depends(require_api_key)):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    version = data.get("version", "")
    sha256  = data.get("sha256", "")
    exe_url = data.get("exe_url", "")
    if not version or not exe_url:
        raise HTTPException(status_code=422, detail="version and exe_url required")
    background_tasks.add_task(_download_and_cache, version, sha256, exe_url)
    return {"ok": True, "version": version}
