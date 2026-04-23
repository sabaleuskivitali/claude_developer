"""
Build and sign a BootstrapProfile from a DeploymentContext.
"""
import secrets
from datetime import datetime, timezone, timedelta

from bootstrap.crypto import sign_profile
from bootstrap.schema import (
    BootstrapEndpoints,
    BootstrapEnrollment,
    BootstrapProfile,
    BootstrapTrust,
    DeploymentContext,
    SignedBootstrapProfile,
)

_PROFILE_TTL_DAYS    = 180
_TOKEN_TTL_DAYS      = 7


def generate_profile(ctx: DeploymentContext) -> SignedBootstrapProfile:
    """
    Build a signed BootstrapProfile for the given DeploymentContext.
    Caller is responsible for persisting the result via store.create().
    """
    if not ctx.server_url:
        raise ValueError("server_url is required in DeploymentContext")
    if not ctx.tenant_id:
        raise ValueError("tenant_id is required in DeploymentContext")

    now     = datetime.now(timezone.utc)
    expires = now + timedelta(days=_PROFILE_TTL_DAYS)

    profile = BootstrapProfile(
        tenant_id   = ctx.tenant_id,
        site_id     = ctx.site_id,
        server_name = ctx.server_name,
        issued_at  = now.isoformat(),
        expires_at = expires.isoformat(),
        endpoints  = BootstrapEndpoints(
            primary   = ctx.server_url,
            policy    = f"{ctx.server_url.rstrip('/')}/api/v1/policy",
        ),
        trust      = BootstrapTrust(),
        enrollment = BootstrapEnrollment(
            token        = secrets.token_urlsafe(32),
            csr_endpoint = f"{ctx.server_url.rstrip('/')}/api/v1/bootstrap/enroll",
            expires_at   = (now + timedelta(days=_TOKEN_TTL_DAYS)).isoformat(),
        ),
    )
    return sign_profile(profile)
