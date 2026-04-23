"""
Aggregate all probes into a DeploymentContext.
"""
import os

from bootstrap.schema import DeploymentContext
from bootstrap.scanner.domain_probe import probe_domain
from bootstrap.scanner.network_probe import probe_network
from bootstrap.scanner.connectivity_probe import probe_connectivity


def build_context(
    server_url: str = "",
    tenant_id: str = "",
    site_id: str = "default",
    domain_hint: str | None = None,
) -> DeploymentContext:
    """
    Run all probes and return a DeploymentContext.
    server_url / tenant_id are passed in (read from env by caller, not hardcoded here).
    """
    domain_hint = domain_hint or os.environ.get("DOMAIN_HINT")
    server_url  = server_url  or os.environ.get("SERVER_URL", "")
    tenant_id   = tenant_id   or os.environ.get("TENANT_ID", "default")

    domain_result  = probe_domain(domain_hint)
    network_result = probe_network()
    conn_result    = probe_connectivity()

    return DeploymentContext(
        has_ad       = domain_result["type"] == "ad",
        ad_domain    = domain_result.get("domain"),
        has_aad      = False,  # AAD detection from server side not applicable in MVP
        l2_reachable = network_result["l2_reachable"],
        has_internet = conn_result["has_internet"],
        is_airgapped = conn_result["is_airgapped"],
        server_url   = server_url,
        tenant_id    = tenant_id,
        site_id      = site_id,
    )
