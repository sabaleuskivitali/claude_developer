"""
Bootstrap API — profile generation, approval, publishing, and agent enrollment.

Public endpoints (no API key): /active, /enroll
Protected endpoints: all others (require API key).
"""
import os
import secrets
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from auth import require_api_key
import bootstrap.store as store
from bootstrap.crypto import export_public_key_pem
from bootstrap.generator import generate_profile
from bootstrap.scanner import build_context
from bootstrap.schema import SignedBootstrapProfile

router  = APIRouter(prefix="/api/v1/bootstrap")
limiter = Limiter(key_func=get_remote_address)

# ---------------------------------------------------------------------------
# Public endpoints — no auth (agents call these before they have credentials)
# ---------------------------------------------------------------------------

@router.get("/active")
async def get_active_profile(request: Request):
    """Return the currently active signed bootstrap profile."""
    signed = await store.get_active(request.app.state.db)
    if not signed:
        raise HTTPException(404, "No active bootstrap profile")
    return signed.model_dump()


@router.post("/enroll")
@limiter.limit("10/minute")
async def enroll_agent(request: Request, body: dict):
    """
    Exchange enrollment token for agent registration.
    Body: {"machine_id": "...", "token": "..."}
    Returns: {"enrolled": true, "api_key": "..."}
    The enrollment token is single-use. A new independent API key is issued on success.
    """
    machine_id = body.get("machine_id", "")
    token      = body.get("token", "")
    method     = body.get("method", "unknown")

    if not machine_id or not token:
        raise HTTPException(400, "machine_id and token required")

    row = await request.app.state.db.fetchrow(
        """
        SELECT et.profile_id::TEXT, et.used, et.expires_at, bp.status
          FROM enrollment_tokens et
          JOIN bootstrap_profiles bp ON bp.profile_id = et.profile_id
         WHERE et.token = $1
        """,
        token,
    )
    # Return identical error for "not found" and "used" to avoid token enumeration
    if not row or row["used"]:
        raise HTTPException(403, "Invalid or expired enrollment token")
    if row["expires_at"] and row["expires_at"].astimezone(timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(403, "Invalid or expired enrollment token")
    if row["status"] not in ("active", "published"):
        raise HTTPException(403, "Bootstrap profile not active")

    # Generate a new independent API key — enrollment token is not reused
    api_key = secrets.token_urlsafe(32)

    # Mark enrollment token used atomically with API key issuance
    await request.app.state.db.execute(
        "UPDATE enrollment_tokens SET used = TRUE, used_at = NOW(), machine_id = $1 WHERE token = $2",
        machine_id, token,
    )
    await store.record_agent_enrollment(
        request.app.state.db, machine_id, row["profile_id"], method
    )
    # Store the issued API key alongside the machine's bootstrap state
    await request.app.state.db.execute(
        "UPDATE agent_bootstrap_state SET cert_expires = NOW() + INTERVAL '180 days' WHERE machine_id = $1",
        machine_id,
    )
    # Persist API key in a dedicated table (or reuse commands table as a simple kv store)
    await request.app.state.db.execute(
        """
        INSERT INTO agent_api_keys (machine_id, api_key, issued_at, expires_at)
        VALUES ($1, $2, NOW(), NOW() + INTERVAL '180 days')
        ON CONFLICT (machine_id) DO UPDATE
            SET api_key = EXCLUDED.api_key, issued_at = EXCLUDED.issued_at, expires_at = EXCLUDED.expires_at
        """,
        machine_id, api_key,
    )

    return {"enrolled": True, "machine_id": machine_id, "api_key": api_key}


# ---------------------------------------------------------------------------
# Protected endpoints — require API key
# ---------------------------------------------------------------------------

@router.post("/generate", dependencies=[Depends(require_api_key)])
async def generate(request: Request, body: dict):
    """
    Scan environment and generate a new bootstrap profile.
    Body (all optional): {"server_url": "...", "tenant_id": "...", "site_id": "..."}
    """
    ctx = build_context(
        server_url  = body.get("server_url", ""),
        tenant_id   = body.get("tenant_id", ""),
        site_id     = body.get("site_id", "default"),
    )
    signed     = generate_profile(ctx)
    profile_id = await store.create(request.app.state.db, signed, ctx)

    # Create enrollment token in DB
    profile    = signed.get_profile()
    token      = profile.enrollment.token
    expires_at = profile.enrollment.expires_at
    await request.app.state.db.execute(
        "INSERT INTO enrollment_tokens (token, profile_id, expires_at) VALUES ($1, $2::UUID, $3::TIMESTAMPTZ)",
        token, profile_id, expires_at,
    )

    return {
        "profile_id":  profile_id,
        "status":      "pending",
        "context":     ctx.model_dump(),
        "signed":      signed.model_dump(),
    }


@router.post("/{profile_id}/approve", dependencies=[Depends(require_api_key)])
async def approve(profile_id: str, request: Request):
    ok = await store.approve(request.app.state.db, profile_id)
    if not ok:
        raise HTTPException(400, "Transition not allowed or profile not found")
    return {"profile_id": profile_id, "status": "approved"}


@router.post("/{profile_id}/publish", dependencies=[Depends(require_api_key)])
async def publish(profile_id: str, request: Request):
    ok = await store.publish(request.app.state.db, profile_id)
    if not ok:
        raise HTTPException(400, "Transition not allowed or profile not found")
    return {"profile_id": profile_id, "status": "active"}


@router.post("/{profile_id}/revoke", dependencies=[Depends(require_api_key)])
async def revoke(profile_id: str, request: Request):
    await store.revoke(request.app.state.db, profile_id)
    return {"profile_id": profile_id, "status": "revoked"}


@router.get("/profiles", dependencies=[Depends(require_api_key)])
async def list_profiles(request: Request):
    rows = await store.list_profiles(request.app.state.db)
    return {"profiles": rows}


@router.get("/{profile_id}", dependencies=[Depends(require_api_key)])
async def get_profile(profile_id: str, request: Request):
    row = await store.get_by_id(request.app.state.db, profile_id)
    if not row:
        raise HTTPException(404, "Profile not found")
    return row


@router.get("/pubkey", dependencies=[Depends(require_api_key)])
async def get_pubkey():
    """Return the CA public key PEM — embed this in the agent binary at build time."""
    return {"pem": export_public_key_pem()}


@router.get("/agents", dependencies=[Depends(require_api_key)])
async def list_bootstrap_agents(request: Request):
    rows = await request.app.state.db.fetch(
        """
        SELECT machine_id, profile_id::TEXT, method, enrolled_at, cert_expires, status, updated_at
          FROM agent_bootstrap_state
         ORDER BY updated_at DESC
        """
    )
    return {"agents": [dict(r) for r in rows]}
