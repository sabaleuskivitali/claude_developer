import asyncpg
import asyncio
import json
import os
from models import EventIn, HeartbeatIn

EVENT_COLUMNS = (
    "event_id", "session_id", "machine_id", "user_id",
    "timestamp_utc", "synced_ts", "drift_ms", "drift_rate_ppm",
    "sequence_idx", "layer", "event_type",
    "process_name", "app_version", "window_title", "window_class",
    "element_type", "element_name", "element_auto_id",
    "case_id", "screenshot_path", "screenshot_dhash", "capture_reason",
    "log_source", "log_level", "raw_message", "message_hash",
    "document_path", "document_name", "payload",
)

INSERT_SQL = f"""
    INSERT INTO events ({", ".join(EVENT_COLUMNS)})
    VALUES ({", ".join(f"${i+1}" for i in range(len(EVENT_COLUMNS)))})
    ON CONFLICT (event_id) DO NOTHING
"""

MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS commands (
        id          BIGSERIAL PRIMARY KEY,
        command_id  UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
        machine_id  TEXT NOT NULL,
        command     TEXT NOT NULL,
        params      JSONB DEFAULT '{}',
        issued_at   TIMESTAMPTZ DEFAULT NOW(),
        issued_by   TEXT DEFAULT 'manual',
        acked_at    TIMESTAMPTZ,
        status      TEXT DEFAULT 'pending',
        message     TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_commands_pending ON commands (machine_id, issued_at)
        WHERE status = 'pending';
    """,
    """
    CREATE TABLE IF NOT EXISTS install_errors (
        id            BIGSERIAL PRIMARY KEY,
        machine_id    TEXT,
        stage         TEXT NOT NULL,
        error         TEXT NOT NULL,
        os_version    TEXT,
        agent_version TEXT,
        ts            TIMESTAMPTZ NOT NULL,
        received_at   TIMESTAMPTZ DEFAULT NOW(),
        payload       JSONB
    );
    CREATE INDEX IF NOT EXISTS idx_install_errors_machine ON install_errors (machine_id, received_at DESC);
    """,
    """
    CREATE TABLE IF NOT EXISTS etl_status (
        id          BIGSERIAL PRIMARY KEY,
        run_at      TIMESTAMPTZ DEFAULT NOW(),
        files       INTEGER NOT NULL DEFAULT 0,
        rows        INTEGER NOT NULL DEFAULT 0,
        duration_ms INTEGER NOT NULL DEFAULT 0,
        error       TEXT
    );
    """,
    # Views — re-applied on every startup (CREATE OR REPLACE is idempotent)
    """
    CREATE OR REPLACE VIEW heartbeat_drift AS
    SELECT
        session_id,
        synced_ts AS hb_ts,
        drift_ms,
        drift_rate_ppm,
        LEAD(synced_ts) OVER (PARTITION BY session_id ORDER BY synced_ts) AS next_hb_ts
    FROM events
    WHERE event_type = 'HeartbeatPulse';
    """,
    """
    CREATE OR REPLACE VIEW events_corrected AS
    SELECT
        e.*,
        e.synced_ts + COALESCE(
            (h.drift_ms + (h.drift_rate_ppm * (e.synced_ts - h.hb_ts) / 1000000.0))::BIGINT,
            e.drift_ms
        ) AS server_ts
    FROM events e
    LEFT JOIN LATERAL (
        SELECT drift_ms, drift_rate_ppm, hb_ts
        FROM heartbeat_drift h
        WHERE h.session_id = e.session_id AND h.hb_ts <= e.synced_ts
        ORDER BY h.hb_ts DESC LIMIT 1
    ) h ON true;
    """,
    """
    CREATE OR REPLACE VIEW machine_status AS
    WITH last_hb AS (
        SELECT DISTINCT ON (machine_id)
            machine_id,
            synced_ts AS last_hb_ts,
            NOW() - TO_TIMESTAMP(synced_ts / 1000.0) AS since,
            (payload->>'events_buffered')::INT AS buffered,
            (payload->>'drift_ms')::INT        AS drift_ms,
            (payload->>'ntp_server_used')      AS ntp_server
        FROM events
        WHERE event_type = 'HeartbeatPulse'
        ORDER BY machine_id, synced_ts DESC
    )
    SELECT
        machine_id, last_hb_ts,
        EXTRACT(EPOCH FROM since)::INT AS lag_seconds,
        buffered, drift_ms, ntp_server,
        CASE
            WHEN since < INTERVAL '2 minutes'  THEN 'online'
            WHEN since < INTERVAL '15 minutes' THEN 'warning'
            ELSE 'offline'
        END AS status
    FROM last_hb;
    """,
    """
    CREATE OR REPLACE VIEW agent_performance AS
    SELECT
        machine_id,
        synced_ts AS snapshot_ts,
        (payload->>'agent_version')             AS version,
        (payload->>'process_cpu_pct')::FLOAT    AS cpu_pct,
        (payload->>'process_ram_mb')::FLOAT     AS ram_mb,
        (payload->>'sqlite_size_mb')::FLOAT     AS db_mb,
        (payload->>'screenshots_size_mb')::FLOAT AS screenshots_mb,
        (payload->>'events_pending')::INT       AS pending,
        (payload->>'events_failed')::INT        AS failed,
        (payload->>'events_rate_per_min')::INT  AS rate_per_min,
        (payload->>'ntp_drift_ms')::INT         AS drift_ms,
        payload->'layer_stats'                  AS layer_stats
    FROM events
    WHERE event_type = 'PerformanceSnapshot'
    ORDER BY synced_ts DESC;
    """,
]


async def create_pool() -> asyncpg.Pool:
    pool = await asyncpg.create_pool(
        os.environ["POSTGRES_DSN"],
        min_size=2,
        max_size=20,
        statement_cache_size=0,  # pgBouncer transaction mode drops server-side prepared statements
    )
    async with pool.acquire() as conn:
        for sql in MIGRATIONS:
            await conn.execute(sql)
    return pool


def _event_to_record(e: EventIn) -> tuple:
    return (
        str(e.event_id), str(e.session_id), e.machine_id, e.user_id,
        e.timestamp_utc, e.synced_ts, e.drift_ms, e.drift_rate_ppm,
        e.sequence_idx, e.layer, e.event_type,
        e.process_name, e.app_version, e.window_title, e.window_class,
        e.element_type, e.element_name, e.element_auto_id,
        e.case_id, e.screenshot_path, e.screenshot_dhash, e.capture_reason,
        e.log_source, e.log_level, e.raw_message, e.message_hash,
        e.document_path, e.document_name,
        json.dumps(e.payload),
    )


async def bulk_insert_events(pool: asyncpg.Pool, events: list[EventIn]) -> int:
    records = [_event_to_record(e) for e in events]
    async with pool.acquire() as conn:
        result = await conn.executemany(INSERT_SQL, records)
    # executemany returns "INSERT 0 N" style string; just return count
    return len(records)


def heartbeat_to_event(h: HeartbeatIn) -> dict:
    return {
        "event_id": str(__import__("uuid").uuid4()),
        "session_id": str(h.session_id),
        "machine_id": h.machine_id,
        "user_id": h.user_id,
        "timestamp_utc": h.client_ts,
        "synced_ts": h.client_ts,
        "drift_ms": h.drift_ms,
        "drift_rate_ppm": h.drift_rate_ppm,
        "sequence_idx": -1,
        "layer": "agent",
        "event_type": "HeartbeatPulse",
        "payload": h.model_dump(),
    }


class EventQueue:
    def __init__(self, pool: asyncpg.Pool, max_size: int = 100_000, flush_interval: float = 1.0):
        self._pool = pool
        self._queue: asyncio.Queue[EventIn] = asyncio.Queue(maxsize=max_size)
        self._flush_interval = flush_interval
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._worker())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._flush()

    async def put(self, event: EventIn):
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            raise RuntimeError("Event queue full")

    async def _flush(self):
        batch = []
        while not self._queue.empty():
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if batch:
            await bulk_insert_events(self._pool, batch)

    async def _worker(self):
        while True:
            await asyncio.sleep(self._flush_interval)
            await self._flush()
