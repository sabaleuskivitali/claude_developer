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
