"""
PostgreSQL persistence for bootstrap profiles with a simple state machine.

States: pending → approved → published → active → expired | revoked
"""
from __future__ import annotations

from datetime import datetime, timezone
import asyncpg

from bootstrap.schema import SignedBootstrapProfile, DeploymentContext

_VALID_TRANSITIONS: dict[str, list[str]] = {
    "pending":   ["approved", "revoked"],
    "approved":  ["published", "revoked"],
    "published": ["active", "revoked"],
    "active":    ["expired", "revoked"],
    "expired":   [],
    "revoked":   [],
}


async def create(
    pool: asyncpg.Pool,
    signed: SignedBootstrapProfile,
    ctx: DeploymentContext,
) -> str:
    """Persist a new profile with status=pending. Returns profile_id (UUID str)."""
    profile = signed.get_profile()
    expires_at = datetime.fromisoformat(profile.expires_at).astimezone(timezone.utc)
    row = await pool.fetchrow(
        """
        INSERT INTO bootstrap_profiles
            (tenant_id, site_id, expires_at, status, signed_data, signature, deployment_context)
        VALUES ($1, $2, $3, 'pending', $4, $5, $6::JSONB)
        RETURNING profile_id::TEXT
        """,
        profile.tenant_id,
        profile.site_id,
        expires_at,
        signed.signed_data,
        signed.signature,
        ctx.model_dump_json(),
    )
    return row["profile_id"]


async def transition(pool: asyncpg.Pool, profile_id: str, to_status: str) -> bool:
    """Advance profile to to_status. Returns False if transition not allowed."""
    row = await pool.fetchrow(
        "SELECT status FROM bootstrap_profiles WHERE profile_id = $1::UUID",
        profile_id,
    )
    if not row:
        return False
    current = row["status"]
    if to_status not in _VALID_TRANSITIONS.get(current, []):
        return False
    await pool.execute(
        "UPDATE bootstrap_profiles SET status = $1 WHERE profile_id = $2::UUID",
        to_status, profile_id,
    )
    return True


async def approve(pool: asyncpg.Pool, profile_id: str) -> bool:
    return await transition(pool, profile_id, "approved")


async def publish(pool: asyncpg.Pool, profile_id: str) -> bool:
    """
    Publish: set this profile to published, demote any other published/active
    profile to expired so only one is active at a time.
    """
    ok = await transition(pool, profile_id, "published")
    if ok:
        await pool.execute(
            """
            UPDATE bootstrap_profiles
               SET status = 'expired'
             WHERE profile_id != $1::UUID
               AND status IN ('published', 'active')
            """,
            profile_id,
        )
        # Immediately activate the newly published profile
        await pool.execute(
            "UPDATE bootstrap_profiles SET status = 'active' WHERE profile_id = $1::UUID",
            profile_id,
        )
    return ok


async def revoke(pool: asyncpg.Pool, profile_id: str) -> bool:
    """Revoke from any non-terminal state."""
    result = await pool.execute(
        "UPDATE bootstrap_profiles SET status = 'revoked' WHERE profile_id = $1::UUID AND status NOT IN ('revoked', 'expired')",
        profile_id,
    )
    return result != "UPDATE 0"


async def get_active(pool: asyncpg.Pool) -> SignedBootstrapProfile | None:
    """Return the currently active signed profile, or None."""
    row = await pool.fetchrow(
        """
        SELECT signed_data, signature
          FROM bootstrap_profiles
         WHERE status = 'active'
           AND expires_at > NOW()
         ORDER BY created_at DESC
         LIMIT 1
        """
    )
    if not row:
        return None
    return SignedBootstrapProfile(signed_data=row["signed_data"], signature=row["signature"])


async def get_by_id(pool: asyncpg.Pool, profile_id: str) -> dict | None:
    row = await pool.fetchrow(
        """
        SELECT profile_id::TEXT, tenant_id, site_id, status,
               signed_data, signature, expires_at, created_at
          FROM bootstrap_profiles
         WHERE profile_id = $1::UUID
        """,
        profile_id,
    )
    return dict(row) if row else None


async def list_profiles(pool: asyncpg.Pool, limit: int = 20) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT profile_id::TEXT, tenant_id, site_id, status,
               expires_at, created_at
          FROM bootstrap_profiles
         ORDER BY created_at DESC
         LIMIT $1
        """,
        limit,
    )
    return [dict(r) for r in rows]


async def record_agent_enrollment(
    pool_or_conn: asyncpg.Pool | asyncpg.Connection,
    machine_id: str,
    profile_id: str,
    method: str,
) -> None:
    await pool_or_conn.execute(
        """
        INSERT INTO agent_bootstrap_state (machine_id, profile_id, method, enrolled_at, status)
        VALUES ($1, $2::UUID, $3, NOW(), 'active')
        ON CONFLICT (machine_id) DO UPDATE
            SET profile_id  = EXCLUDED.profile_id,
                method      = EXCLUDED.method,
                enrolled_at = EXCLUDED.enrolled_at,
                status      = 'active',
                updated_at  = NOW()
        """,
        machine_id, profile_id, method,
    )
