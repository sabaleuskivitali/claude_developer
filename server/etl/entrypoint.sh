#!/bin/bash
set -e

INTERVAL=${ETL_INTERVAL_MINUTES:-60}

echo "ETL worker started. Interval: ${INTERVAL} minutes."
echo "Share path: ${SMB_SHARE_PATH}"

# Run once at startup, then loop
while true; do
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Running ETL..."
    python /app/etl/load_events.py || echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ETL failed (will retry next cycle)"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ETL done. Sleeping ${INTERVAL}m."
    sleep $(( INTERVAL * 60 ))
done
