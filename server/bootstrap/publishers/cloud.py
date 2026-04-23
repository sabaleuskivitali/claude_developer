import json
import logging
import os
import ssl
import urllib.error
import urllib.request

from bootstrap.schema import SignedBootstrapProfile

log = logging.getLogger(__name__)

_CLOUD_URL = os.getenv("CLOUD_BOOTSTRAP_URL", "").rstrip("/")
_API_KEY = os.getenv("CLOUD_BOOTSTRAP_API_KEY", "")

# Self-signed cert — skip CA verification; agent does thumbprint pinning on download.
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def upload(signed: SignedBootstrapProfile) -> str | None:
    if not _CLOUD_URL or not _API_KEY:
        log.info("Cloud publisher not configured — skipping (set CLOUD_BOOTSTRAP_URL + CLOUD_BOOTSTRAP_API_KEY)")
        return None
    try:
        payload = signed.model_dump_json().encode()
        req = urllib.request.Request(
            f"{_CLOUD_URL}/upload",
            data=payload,
            headers={"Content-Type": "application/json", "X-Api-Key": _API_KEY},
            method="POST",
        )
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=15) as resp:
            data = json.loads(resp.read())
            url = data["url"]
            log.info("Cloud publish OK → %s", url)
            return url
    except Exception as exc:
        log.warning("Cloud publish failed: %s", exc)
        return None
