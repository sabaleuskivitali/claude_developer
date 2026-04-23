"""
Cloud publisher — BACKLOG (not implemented).

When implemented, this module will:
  - Upload the signed profile to a cloud relay (S3/GCS/Azure Blob or custom API)
  - Return a download URL + token for the agent to fetch the profile
  - Support cross-WAN bootstrap without L2 mDNS or GPO

Interface is defined here so the cascade orchestrator can reference it.
"""
import logging

from bootstrap.schema import SignedBootstrapProfile

log = logging.getLogger(__name__)


def upload(signed: SignedBootstrapProfile) -> str | None:
    """
    Upload profile to cloud relay. Returns download URL, or None on failure.
    Not implemented — returns None and logs a warning.
    """
    log.warning("Cloud publisher not implemented (backlog) — skipping cloud upload")
    return None
