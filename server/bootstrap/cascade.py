"""
Deployment Cascade orchestrator.

Priority order (first available wins):
  1. GPO / Intune   — if has_ad or has_aad
  2. Cloud-Assisted — if has_internet (stub, backlog)
  3. DNS-SD / mDNS  — if l2_reachable
  4. Offline Package — always available as fallback

Returns a CascadeResult describing which methods were activated.
"""
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from bootstrap.schema import DeploymentContext, SignedBootstrapProfile

log = logging.getLogger(__name__)


@dataclass
class CascadeResult:
    methods_activated: list[str] = field(default_factory=list)
    gpo_package_path: str | None = None
    offline_package_path: str | None = None
    cloud_url: str | None = None
    dnssd_active: bool = False


def deploy(
    signed: SignedBootstrapProfile,
    ctx: DeploymentContext,
    output_dir: str,
    api_port: int = 49200,
) -> CascadeResult:
    """
    Activate all applicable deployment methods for the given context.
    output_dir: where to write GPO/offline packages.
    """
    result = CascadeResult()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Method 1: GPO (domain present — covers any network topology)
    if ctx.has_ad or ctx.has_aad:
        try:
            from bootstrap.publishers.gpo import generate_gpo_package
            gpo_path = generate_gpo_package(signed, str(out / "gpo_package"))
            result.gpo_package_path = str(gpo_path)
            result.methods_activated.append("gpo")
            log.info("Cascade: GPO package written to %s", gpo_path)
        except Exception as e:
            log.error("Cascade: GPO publisher failed: %s", e)

    # Method 2: Cloud-Assisted (internet available — universal fallback)
    if ctx.has_internet:
        try:
            from bootstrap.publishers.cloud import upload
            url = upload(signed)
            if url:
                result.cloud_url = url
                result.methods_activated.append("cloud")
                log.info("Cascade: Cloud upload succeeded: %s", url)
            else:
                log.info("Cascade: Cloud publisher not available (backlog)")
        except Exception as e:
            log.error("Cascade: Cloud publisher failed: %s", e)

    # Method 3: DNS-SD (L2 reachable)
    if ctx.l2_reachable:
        try:
            from bootstrap.publishers.dnssd import start as dnssd_start
            dnssd_start(signed, api_port)
            result.dnssd_active = True
            result.methods_activated.append("dnssd")
            log.info("Cascade: DNS-SD advertised on port %d", api_port)
        except Exception as e:
            log.error("Cascade: DNS-SD publisher failed: %s", e)

    # Method 4: Offline Package (always — IT fallback)
    try:
        from bootstrap.publishers.offline import generate_offline_package
        offline_path = generate_offline_package(signed, str(out / "bootstrap_offline.zip"))
        result.offline_package_path = str(offline_path)
        result.methods_activated.append("offline")
        log.info("Cascade: Offline package written to %s", offline_path)
    except Exception as e:
        log.error("Cascade: Offline publisher failed: %s", e)

    if not result.methods_activated:
        log.error("Cascade: no deployment method succeeded")
    else:
        log.info("Cascade: activated methods: %s", result.methods_activated)

    return result
