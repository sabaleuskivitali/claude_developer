-- Task Mining Agent — PostgreSQL schema
-- Applied automatically by docker-entrypoint-initdb.d on first start

CREATE TABLE IF NOT EXISTS events (
    id               BIGSERIAL PRIMARY KEY,
    event_id         UUID NOT NULL UNIQUE,
    session_id       UUID NOT NULL,
    machine_id       TEXT NOT NULL,
    user_id          TEXT NOT NULL,
    timestamp_utc    BIGINT NOT NULL,
    synced_ts        BIGINT NOT NULL,
    drift_ms         INTEGER NOT NULL DEFAULT 0,
    drift_rate_ppm   FLOAT   NOT NULL DEFAULT 0,
    sequence_idx     INTEGER NOT NULL,
    layer            TEXT NOT NULL,
    event_type       TEXT NOT NULL,
    process_name     TEXT,
    app_version      TEXT,
    window_title     TEXT,
    window_class     TEXT,
    element_type     TEXT,
    element_name     TEXT,
    element_auto_id  TEXT,
    case_id          TEXT,
    screenshot_path  TEXT,
    screenshot_dhash BIGINT,
    capture_reason   TEXT,
    -- Layer Д — app logs
    log_source       TEXT,
    log_level        TEXT,
    raw_message      TEXT,
    message_hash     TEXT,
    document_path    TEXT,
    document_name    TEXT,
    -- Vision (filled by pipeline)
    vision_done      BOOLEAN DEFAULT FALSE,
    vision_skipped   BOOLEAN DEFAULT FALSE,
    vision_task_label   TEXT,
    vision_app_context  TEXT,
    vision_action_type  TEXT,
    vision_case_id      TEXT,
    vision_is_commit    BOOLEAN,
    vision_cognitive    TEXT,
    vision_confidence   FLOAT,
    vision_auto_notes   TEXT,
    -- Computed
    resolved_case_id TEXT GENERATED ALWAYS AS (
        COALESCE(vision_case_id, case_id, document_name)
    ) STORED,
    payload          JSONB NOT NULL,
    loaded_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_session     ON events (session_id, synced_ts);
CREATE INDEX IF NOT EXISTS idx_machine_ts  ON events (machine_id, synced_ts);
CREATE INDEX IF NOT EXISTS idx_case        ON events (resolved_case_id) WHERE resolved_case_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_vision_todo ON events (vision_done, layer)
    WHERE layer = 'visual' AND vision_done = FALSE AND vision_skipped = FALSE;
CREATE INDEX IF NOT EXISTS idx_event_type  ON events (event_type, synced_ts);

-- Drift interpolation: heartbeat reference points per session
CREATE OR REPLACE VIEW heartbeat_drift AS
SELECT
    session_id,
    synced_ts                                                      AS hb_ts,
    drift_ms,
    drift_rate_ppm,
    LEAD(synced_ts) OVER (PARTITION BY session_id ORDER BY synced_ts) AS next_hb_ts
FROM events
WHERE event_type = 'HeartbeatPulse';

-- Corrected timestamps using drift interpolation
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
    WHERE h.session_id = e.session_id
      AND h.hb_ts <= e.synced_ts
    ORDER BY h.hb_ts DESC
    LIMIT 1
) h ON true;

-- Agent performance snapshots
CREATE OR REPLACE VIEW agent_performance AS
SELECT
    machine_id,
    synced_ts                                         AS snapshot_ts,
    (payload->>'agent_version')                       AS version,
    (payload->>'process_cpu_pct')::FLOAT              AS cpu_pct,
    (payload->>'process_ram_mb')::FLOAT               AS ram_mb,
    (payload->>'sqlite_size_mb')::FLOAT               AS db_mb,
    (payload->>'screenshots_size_mb')::FLOAT          AS screenshots_mb,
    (payload->>'events_pending')::INT                 AS pending,
    (payload->>'events_failed')::INT                  AS failed,
    (payload->>'events_rate_per_min')::INT            AS rate_per_min,
    (payload->>'ntp_drift_ms')::INT                   AS drift_ms,
    (payload->>'smb_last_sync_ago_min')::INT          AS sync_lag_min,
    payload->'layer_stats'                            AS layer_stats
FROM events
WHERE event_type = 'PerformanceSnapshot'
ORDER BY synced_ts DESC;

-- App log correlation with task labels
CREATE OR REPLACE VIEW task_log_correlation AS
SELECT
    e.user_id,
    e.resolved_case_id,
    v.vision_task_label,
    al.log_source,
    al.log_level,
    al.raw_message,
    al.synced_ts - e.synced_ts AS offset_ms
FROM events e
JOIN events v  ON v.session_id = e.session_id
             AND ABS(v.synced_ts - e.synced_ts) < 5000
             AND v.layer = 'visual'
             AND v.vision_task_label IS NOT NULL
JOIN events al ON al.session_id = e.session_id
             AND ABS(al.synced_ts - e.synced_ts) < 30000
             AND al.layer = 'applogs'
WHERE e.layer = 'window'
  AND e.event_type = 'WindowActivated';

-- Machine status helper (used by manage.py status)
CREATE OR REPLACE VIEW machine_status AS
WITH last_hb AS (
    SELECT DISTINCT ON (machine_id)
        machine_id,
        synced_ts                            AS last_hb_ts,
        NOW() - TO_TIMESTAMP(synced_ts / 1000.0) AS since,
        (payload->>'events_buffered')::INT   AS buffered,
        (payload->>'drift_ms')::INT          AS drift_ms,
        (payload->>'ntp_server_used')        AS ntp_server
    FROM events
    WHERE event_type = 'HeartbeatPulse'
    ORDER BY machine_id, synced_ts DESC
)
SELECT
    machine_id,
    last_hb_ts,
    EXTRACT(EPOCH FROM since)::INT          AS lag_seconds,
    buffered,
    drift_ms,
    ntp_server,
    CASE
        WHEN since < INTERVAL '2 minutes'  THEN 'online'
        WHEN since < INTERVAL '15 minutes' THEN 'warning'
        ELSE 'offline'
    END                                     AS status
FROM last_hb;
