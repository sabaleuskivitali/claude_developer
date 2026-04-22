-- Migration v2: HTTP API infrastructure
-- Run manually: psql $POSTGRES_DSN -f schema_v2.sql
-- Applied automatically by diag_api on startup via db.py MIGRATIONS

CREATE TABLE IF NOT EXISTS commands (
    id          BIGSERIAL PRIMARY KEY,
    command_id  UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    machine_id  TEXT NOT NULL,
    command     TEXT NOT NULL,          -- restart | stop | start | status_dump | update_config
    params      JSONB DEFAULT '{}',
    issued_at   TIMESTAMPTZ DEFAULT NOW(),
    issued_by   TEXT DEFAULT 'manual',
    acked_at    TIMESTAMPTZ,
    status      TEXT DEFAULT 'pending', -- pending | ok | error | expired
    message     TEXT
);

CREATE INDEX IF NOT EXISTS idx_commands_pending ON commands (machine_id, issued_at)
    WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS install_errors (
    id            BIGSERIAL PRIMARY KEY,
    machine_id    TEXT,
    stage         TEXT NOT NULL,        -- defender | scheduled_task | extension | ...
    error         TEXT NOT NULL,
    os_version    TEXT,
    agent_version TEXT,
    ts            TIMESTAMPTZ NOT NULL,
    received_at   TIMESTAMPTZ DEFAULT NOW(),
    payload       JSONB
);

CREATE INDEX IF NOT EXISTS idx_install_errors_machine ON install_errors (machine_id, received_at DESC);
