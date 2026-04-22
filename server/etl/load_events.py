"""
ETL: scan SMB share for SQLite files, load events into PostgreSQL.

Directory layout on share:
  {share}/{machine_id}/{YYYYMMDD}/events.db

Idempotent: INSERT ... ON CONFLICT (event_id) DO NOTHING.
State file tracks which (machine_id, date, mtime) were already loaded.
"""

import json
import logging
import os
import pathlib
import pickle
import sqlite3
import time

import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

SHARE_PATH  = pathlib.Path(os.environ["SMB_SHARE_PATH"])
POSTGRES_DSN = os.environ["POSTGRES_DSN"]
STATE_FILE  = pathlib.Path(os.environ.get("STATE_DIR", "/app/state")) / "etl_state.pkl"

# SQLite columns to read (must match client schema)
SELECT_COLS = """
    event_id, session_id, machine_id, user_id,
    timestamp_utc, synced_ts, drift_ms, drift_rate_ppm, sequence_idx,
    layer, event_type,
    process_name, app_version, window_title, window_class,
    element_type, element_name, element_auto_id,
    case_id, screenshot_path, screenshot_dhash, capture_reason,
    log_source, log_level, raw_message, message_hash, document_path, document_name,
    payload
"""

INSERT_SQL = """
INSERT INTO events (
    event_id, session_id, machine_id, user_id,
    timestamp_utc, synced_ts, drift_ms, drift_rate_ppm, sequence_idx,
    layer, event_type,
    process_name, app_version, window_title, window_class,
    element_type, element_name, element_auto_id,
    case_id, screenshot_path, screenshot_dhash, capture_reason,
    log_source, log_level, raw_message, message_hash, document_path, document_name,
    payload
) VALUES (
    %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s,
    %s
)
ON CONFLICT (event_id) DO NOTHING
"""


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "rb") as f:
            return pickle.load(f)
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "wb") as f:
        pickle.dump(state, f)


def find_db_files() -> list[pathlib.Path]:
    """Yield all events.db files that are not from today (closed days only)."""
    import datetime
    today = datetime.date.today().strftime("%Y%m%d")
    return [
        p for p in SHARE_PATH.glob("*/*/events.db")
        if p.parent.name != today
    ]


def load_sqlite_file(db_path: pathlib.Path, pg_conn) -> int:
    """Load all events from one SQLite file into PostgreSQL. Returns row count inserted."""
    rows_inserted = 0
    try:
        sq = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        sq.row_factory = sqlite3.Row
        cursor = sq.execute(f"SELECT {SELECT_COLS} FROM events")
        rows = cursor.fetchall()
        sq.close()
    except sqlite3.OperationalError as e:
        log.warning("Cannot open %s: %s", db_path, e)
        return 0

    if not rows:
        return 0

    def clean(v):
        if isinstance(v, str):
            return v.replace('\x00', '').replace('\\u0000', '')
        return v

    batch = []
    for row in rows:
        values = [clean(v) for v in row]
        # payload is TEXT in SQLite, needs to be cast to JSONB — pass as str, psycopg2 handles it
        payload_idx = len(values) - 1
        if values[payload_idx] is None:
            values[payload_idx] = "{}"
        batch.append(tuple(values))

    with pg_conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, INSERT_SQL, batch, page_size=500)
        rows_inserted = cur.rowcount if cur.rowcount != -1 else len(batch)

    pg_conn.commit()
    return rows_inserted


def record_etl_run(pg_conn, files: int, rows: int, duration_ms: int, error: str | None) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO etl_status (files, rows, duration_ms, error) VALUES (%s, %s, %s, %s)",
            (files, rows, duration_ms, error),
        )
    pg_conn.commit()


def run() -> None:
    t_start = time.time()
    state = load_state()
    db_files = find_db_files()

    pg_conn = psycopg2.connect(POSTGRES_DSN)

    if not db_files:
        log.info("No closed-day SQLite files found under %s", SHARE_PATH)
        record_etl_run(pg_conn, 0, 0, 0, None)
        pg_conn.close()
        return

    total_inserted = 0
    files_processed = 0
    run_error = None

    try:
        for db_path in sorted(db_files):
            mtime = db_path.stat().st_mtime
            key = str(db_path)
            if state.get(key) == mtime:
                log.debug("Skip unchanged: %s", db_path)
                continue

            log.info("Loading %s ...", db_path)
            inserted = load_sqlite_file(db_path, pg_conn)
            log.info("  → %d rows inserted", inserted)
            state[key] = mtime
            total_inserted += inserted
            files_processed += 1
    except Exception as e:
        run_error = str(e)
        log.error("ETL error: %s", e)

    duration_ms = int((time.time() - t_start) * 1000)
    pg_conn_status = psycopg2.connect(POSTGRES_DSN)
    record_etl_run(pg_conn_status, files_processed, total_inserted, duration_ms, run_error)
    pg_conn_status.close()

    pg_conn.close()
    save_state(state)
    log.info("ETL complete. Files: %d, rows: %d, duration: %dms", files_processed, total_inserted, duration_ms)


if __name__ == "__main__":
    run()
