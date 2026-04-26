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
    # Bootstrap tables
    """
    CREATE TABLE IF NOT EXISTS bootstrap_profiles (
        id           BIGSERIAL PRIMARY KEY,
        profile_id   UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
        tenant_id    TEXT NOT NULL,
        site_id      TEXT NOT NULL DEFAULT 'default',
        issued_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at   TIMESTAMPTZ NOT NULL,
        status       TEXT NOT NULL DEFAULT 'pending',
        signed_data  TEXT NOT NULL,
        signature    TEXT NOT NULL,
        deployment_context JSONB DEFAULT '{}'::JSONB,
        created_at   TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_bootstrap_status ON bootstrap_profiles (status, created_at DESC);

    CREATE TABLE IF NOT EXISTS enrollment_tokens (
        id          BIGSERIAL PRIMARY KEY,
        token       TEXT NOT NULL UNIQUE,
        profile_id  UUID NOT NULL REFERENCES bootstrap_profiles(profile_id) ON DELETE CASCADE,
        machine_id  TEXT,
        used        BOOLEAN DEFAULT FALSE,
        used_at     TIMESTAMPTZ,
        expires_at  TIMESTAMPTZ NOT NULL,
        created_at  TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_enrollment_token ON enrollment_tokens (token) WHERE used = FALSE;

    CREATE TABLE IF NOT EXISTS agent_api_keys (
        machine_id  TEXT PRIMARY KEY,
        api_key     TEXT NOT NULL,
        issued_at   TIMESTAMPTZ DEFAULT NOW(),
        expires_at  TIMESTAMPTZ NOT NULL
    );

    CREATE TABLE IF NOT EXISTS agent_bootstrap_state (
        machine_id   TEXT PRIMARY KEY,
        profile_id   UUID REFERENCES bootstrap_profiles(profile_id),
        method       TEXT,
        enrolled_at  TIMESTAMPTZ,
        cert_expires TIMESTAMPTZ,
        status       TEXT DEFAULT 'pending',
        updated_at   TIMESTAMPTZ DEFAULT NOW()
    );
    """,
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
    # Multi-use enrollment tokens: track per-(token, machine) pair instead of global used flag
    """
    CREATE TABLE IF NOT EXISTS enrollment_token_uses (
        token      TEXT NOT NULL,
        machine_id TEXT NOT NULL,
        used_at    TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (token, machine_id)
    );
    DROP INDEX IF EXISTS idx_enrollment_token;
    CREATE INDEX IF NOT EXISTS idx_enrollment_token ON enrollment_tokens (token);
    """,
    # Views — DROP + CREATE so column changes always apply cleanly
    """
    DROP VIEW IF EXISTS events_corrected CASCADE;
    DROP VIEW IF EXISTS heartbeat_drift CASCADE;
    DROP VIEW IF EXISTS agent_performance CASCADE;
    DROP VIEW IF EXISTS machine_status CASCADE;
    DROP VIEW IF EXISTS task_log_correlation CASCADE;

    CREATE VIEW heartbeat_drift AS
    SELECT
        session_id,
        synced_ts AS hb_ts,
        drift_ms,
        drift_rate_ppm,
        LEAD(synced_ts) OVER (PARTITION BY session_id ORDER BY synced_ts) AS next_hb_ts
    FROM events
    WHERE event_type = 'HeartbeatPulse';

    CREATE VIEW events_corrected AS
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

    CREATE VIEW machine_status AS
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

    CREATE VIEW agent_performance AS
    SELECT
        machine_id,
        synced_ts AS snapshot_ts,
        (payload->>'agent_version')              AS version,
        (payload->>'process_cpu_pct')::FLOAT     AS cpu_pct,
        (payload->>'process_ram_mb')::FLOAT      AS ram_mb,
        (payload->>'sqlite_size_mb')::FLOAT      AS db_mb,
        (payload->>'screenshots_size_mb')::FLOAT AS screenshots_mb,
        (payload->>'events_pending')::INT        AS pending,
        (payload->>'events_failed')::INT         AS failed,
        (payload->>'events_rate_per_min')::INT   AS rate_per_min,
        (payload->>'ntp_drift_ms')::INT          AS drift_ms,
        payload->'layer_stats'                   AS layer_stats
    FROM events
    WHERE event_type = 'PerformanceSnapshot'
    ORDER BY synced_ts DESC;
    """,
    """
    CREATE TABLE IF NOT EXISTS machine_settings (
        machine_id  TEXT PRIMARY KEY,
        auto_update BOOLEAN NOT NULL DEFAULT TRUE
    );
    """,
    # Vision pipeline: per-run results with full history
    """
    CREATE TABLE IF NOT EXISTS vision_results (
        id                BIGSERIAL PRIMARY KEY,
        event_id          TEXT NOT NULL,
        run_id            UUID NOT NULL,
        model             TEXT NOT NULL,
        prompt_version    TEXT NOT NULL,
        reasoning         TEXT,
        task_label        TEXT,
        app_context       TEXT,
        action_type       TEXT,
        visible_fields    TEXT[],
        case_id_candidate TEXT,
        completion_signal BOOLEAN,
        cognitive_demand  TEXT,
        automation_notes  TEXT,
        confidence        FLOAT,
        processed_at      TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (event_id, run_id)
    );
    CREATE INDEX IF NOT EXISTS idx_vr_event_conf
        ON vision_results (event_id, confidence DESC);
    CREATE INDEX IF NOT EXISTS idx_vr_run
        ON vision_results (run_id, processed_at);
    """,
    # Task sessions and FTE report
    """
    CREATE TABLE IF NOT EXISTS task_sessions (
        session_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id              TEXT NOT NULL,
        machine_id           TEXT NOT NULL,
        task_label           TEXT NOT NULL,
        start_ts             BIGINT NOT NULL,
        end_ts               BIGINT NOT NULL,
        duration_min         FLOAT NOT NULL,
        screenshot_count     INT NOT NULL DEFAULT 0,
        event_count          INT NOT NULL DEFAULT 0,
        case_id              TEXT,
        case_id_candidates   JSONB,
        process_names        TEXT[],
        has_undo             BOOLEAN DEFAULT FALSE,
        has_error            BOOLEAN DEFAULT FALSE,
        event_sequence       TEXT,
        avg_cognitive_demand TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_ts_user_machine
        ON task_sessions (user_id, machine_id, start_ts);
    CREATE INDEX IF NOT EXISTS idx_ts_label
        ON task_sessions (task_label, start_ts);

    CREATE TABLE IF NOT EXISTS fte_report (
        id                   BIGSERIAL PRIMARY KEY,
        report_date          DATE NOT NULL DEFAULT CURRENT_DATE,
        user_id              TEXT NOT NULL,
        machine_id           TEXT NOT NULL,
        task_label           TEXT NOT NULL,
        executions_per_day   FLOAT NOT NULL,
        avg_duration_min     FLOAT NOT NULL,
        pct_workday          FLOAT NOT NULL,
        repeatability        FLOAT NOT NULL,
        exception_rate       FLOAT NOT NULL,
        automation_score     FLOAT NOT NULL,
        automation_type      TEXT NOT NULL,
        fte_saving           FLOAT NOT NULL,
        avg_cognitive_demand TEXT NOT NULL,
        UNIQUE (report_date, user_id, machine_id, task_label)
    );
    """,
    # Meeting recordings pipeline
    """
    CREATE TABLE IF NOT EXISTS meeting_recordings (
        meeting_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        machine_id      TEXT NOT NULL,
        user_id         TEXT NOT NULL,
        started_at      BIGINT NOT NULL,
        ended_at        BIGINT,
        duration_sec    FLOAT,
        mic_path        TEXT,
        loopback_path   TEXT,
        process_name    TEXT,
        window_title    TEXT,
        trigger         TEXT,
        whisper_done    BOOLEAN NOT NULL DEFAULT FALSE,
        whisper_skipped BOOLEAN NOT NULL DEFAULT FALSE,
        created_at      TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_mr_machine
        ON meeting_recordings (machine_id, started_at DESC);
    CREATE INDEX IF NOT EXISTS idx_mr_whisper_todo
        ON meeting_recordings (whisper_done, whisper_skipped)
        WHERE whisper_done = FALSE AND whisper_skipped = FALSE;

    CREATE TABLE IF NOT EXISTS meeting_transcripts (
        id             BIGSERIAL PRIMARY KEY,
        meeting_id     UUID NOT NULL REFERENCES meeting_recordings(meeting_id) ON DELETE CASCADE,
        run_id         UUID NOT NULL,
        whisper_model  TEXT NOT NULL,
        language       TEXT,
        transcript     TEXT,
        segments       JSONB,
        summary        TEXT,
        action_items   JSONB,
        case_id        TEXT,
        processed_at   TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (meeting_id, run_id)
    );
    CREATE INDEX IF NOT EXISTS idx_mt_meeting
        ON meeting_transcripts (meeting_id, processed_at DESC);
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


def _s(v: str | None) -> str | None:
    """Strip null bytes — PostgreSQL UTF-8 rejects \x00."""
    return v.replace("\x00", "") if v else v


def _event_to_record(e: EventIn) -> tuple:
    return (
        str(e.event_id), str(e.session_id), _s(e.machine_id), _s(e.user_id),
        e.timestamp_utc, e.synced_ts, e.drift_ms, e.drift_rate_ppm,
        e.sequence_idx, _s(e.layer), _s(e.event_type),
        _s(e.process_name), _s(e.app_version), _s(e.window_title), _s(e.window_class),
        _s(e.element_type), _s(e.element_name), _s(e.element_auto_id),
        _s(e.case_id), _s(e.screenshot_path), e.screenshot_dhash, _s(e.capture_reason),
        _s(e.log_source), _s(e.log_level), _s(e.raw_message), _s(e.message_hash),
        _s(e.document_path), _s(e.document_name),
        json.dumps(e.payload, default=str).replace("\x00", "").replace("\\u0000", ""),
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
        import logging
        logger = logging.getLogger(__name__)
        while True:
            await asyncio.sleep(self._flush_interval)
            try:
                await self._flush()
            except Exception as exc:
                logger.error("EventQueue flush failed: %s", exc, exc_info=True)
