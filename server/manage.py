#!/usr/bin/env python3
"""
manage.py — CLI for Task Mining Agent server.

Usage:
  python manage.py status
  python manage.py status <machine_id>
  python manage.py restart <machine_id>
  python manage.py restart --offline
  python manage.py restart --all
  python manage.py logs <machine_id> [--errors] [--tail N]
  python manage.py ack <machine_id>
  python manage.py update-config <machine_id> --set KEY=VALUE
  python manage.py perf [<machine_id>] [--warnings]
  python manage.py etl
"""

import argparse
import json
import os
import pathlib
import sys
import uuid
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

DSN = os.environ.get(
    "POSTGRES_DSN",
    "postgresql://diag:diag@localhost:5432/diag",
)
SMB_PATH = pathlib.Path(os.environ.get("SMB_SHARE_PATH", "/mnt/diag"))


def connect():
    return psycopg2.connect(DSN, cursor_factory=psycopg2.extras.RealDictCursor)


def fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m}m"


def status_icon(s: str) -> str:
    return {"online": "🟢", "warning": "🟡", "offline": "🔴", "never": "❓"}.get(s, "?")


# ── status ────────────────────────────────────────────────────────────────────

def cmd_status(args):
    conn = connect()
    with conn.cursor() as cur:
        if args.machine_id:
            cur.execute(
                "SELECT * FROM machine_status WHERE machine_id LIKE %s",
                (args.machine_id + "%",),
            )
        else:
            cur.execute("SELECT * FROM machine_status ORDER BY status, machine_id")
        rows = cur.fetchall()

    if not rows:
        # check if there are any machines at all
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT machine_id FROM events LIMIT 5")
            machines = [r["machine_id"] for r in cur.fetchall()]
        if machines:
            print("No heartbeat received yet. Known machines:")
            for m in machines:
                print(f"  ❓ {m[:16]}...  (never)")
        else:
            print("No data in database yet.")
        return

    print(f"{'MACHINE_ID':<18} {'LAST_SEEN':<20} {'LAG':<10} {'BUFFERED':<10} {'DRIFT_MS':<10} STATUS")
    print("-" * 82)
    for r in rows:
        lag = fmt_duration(r["lag_seconds"] or 0)
        seen = datetime.fromtimestamp(
            r["last_hb_ts"] / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M")
        icon = status_icon(r["status"])
        drift = r["drift_ms"] if r["drift_ms"] is not None else "—"
        buffered = r["buffered"] if r["buffered"] is not None else "—"
        mid = (r["machine_id"] or "")[:16]
        print(f"{mid:<18} {seen:<20} {lag:<10} {str(buffered):<10} {str(drift):<10} {icon} {r['status']}")

    conn.close()


# ── restart ───────────────────────────────────────────────────────────────────

def write_command(machine_id: str, command: str, params: dict = None) -> str:
    cmd_dir = SMB_PATH / machine_id / "cmd"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    command_id = str(uuid.uuid4())
    payload = {
        "command_id": command_id,
        "command": command,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "issued_by": "manage.py",
        "params": params or {},
    }
    (cmd_dir / "pending.json").write_text(json.dumps(payload, indent=2))
    return command_id


def get_machines_by_status(status: str) -> list[str]:
    conn = connect()
    with conn.cursor() as cur:
        cur.execute("SELECT machine_id FROM machine_status WHERE status = %s", (status,))
        rows = cur.fetchall()
    conn.close()
    return [r["machine_id"] for r in rows]


def get_all_machines() -> list[str]:
    conn = connect()
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT machine_id FROM events")
        rows = cur.fetchall()
    conn.close()
    return [r["machine_id"] for r in rows]


def cmd_restart(args):
    if args.machine_id:
        machines = [args.machine_id]
    elif args.offline:
        machines = get_machines_by_status("offline")
    elif args.all:
        machines = get_all_machines()
    else:
        print("Specify machine_id, --offline, or --all")
        sys.exit(1)

    if not machines:
        print("No matching machines.")
        return

    for mid in machines:
        cid = write_command(mid, "restart")
        print(f"  → restart queued for {mid[:16]}... (command_id={cid[:8]}...)")


# ── logs ──────────────────────────────────────────────────────────────────────

def cmd_logs(args):
    conn = connect()
    tail = args.tail or 20
    with conn.cursor() as cur:
        if args.errors:
            cur.execute(
                """
                SELECT synced_ts, event_type, payload
                FROM events
                WHERE machine_id LIKE %s
                  AND event_type IN ('LayerError', 'CommandExecuted', 'UpdateCompleted')
                ORDER BY synced_ts DESC
                LIMIT %s
                """,
                (args.machine_id + "%", tail),
            )
        else:
            cur.execute(
                """
                SELECT synced_ts, event_type, window_title, process_name, payload
                FROM events
                WHERE machine_id LIKE %s
                ORDER BY synced_ts DESC
                LIMIT %s
                """,
                (args.machine_id + "%", tail),
            )
        rows = cur.fetchall()
    conn.close()

    if not rows:
        print("No events found.")
        return

    for r in reversed(rows):
        ts = datetime.fromtimestamp(r["synced_ts"] / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        if args.errors:
            payload = r["payload"] or {}
            msg = payload.get("exception_message") or payload.get("message") or ""
            print(f"{ts}  {r['event_type']:<25} {msg[:80]}")
        else:
            print(
                f"{ts}  {r['event_type']:<25} {(r['process_name'] or ''):<20} {(r['window_title'] or '')[:50]}"
            )


# ── ack ───────────────────────────────────────────────────────────────────────

def cmd_ack(args):
    ack_file = SMB_PATH / args.machine_id / "cmd" / "ack.json"
    if not ack_file.exists():
        print(f"No ack.json found at {ack_file}")
        return
    data = json.loads(ack_file.read_text())
    print(json.dumps(data, indent=2, ensure_ascii=False))


# ── update-config ─────────────────────────────────────────────────────────────

def cmd_update_config(args):
    params = {}
    for kv in args.set:
        if "=" not in kv:
            print(f"Invalid --set value: {kv} (expected KEY=VALUE)")
            sys.exit(1)
        k, v = kv.split("=", 1)
        # try to coerce numeric values
        try:
            v = int(v)
        except ValueError:
            try:
                v = float(v)
            except ValueError:
                pass
        params[k] = v

    cid = write_command(args.machine_id, "update_config", params)
    print(f"update_config queued for {args.machine_id[:16]}... (command_id={cid[:8]}...)")
    print(f"Params: {params}")


# ── perf ──────────────────────────────────────────────────────────────────────

def cmd_perf(args):
    conn = connect()
    with conn.cursor() as cur:
        if args.machine_id:
            cur.execute(
                "SELECT * FROM agent_performance WHERE machine_id LIKE %s ORDER BY snapshot_ts DESC LIMIT 1",
                (args.machine_id + "%",),
            )
        elif args.warnings:
            cur.execute(
                """
                SELECT DISTINCT ON (machine_id) *
                FROM agent_performance
                WHERE pending > 1000
                   OR sync_lag_min > 120
                   OR cpu_pct > 10
                   OR ABS(drift_ms) > 1000
                ORDER BY machine_id, snapshot_ts DESC
                """
            )
        else:
            cur.execute(
                """
                SELECT DISTINCT ON (machine_id) *
                FROM agent_performance
                ORDER BY machine_id, snapshot_ts DESC
                """
            )
        rows = cur.fetchall()
    conn.close()

    if not rows:
        print("No performance snapshots found.")
        return

    print(f"{'MACHINE':<18} {'VER':<8} {'CPU%':<6} {'RAM_MB':<8} {'DB_MB':<8} {'PENDING':<9} {'SYNC_LAG':<10} {'DRIFT_MS':<10} {'RATE/MIN'}")
    print("-" * 95)
    for r in rows:
        mid = (r["machine_id"] or "")[:16]
        lag = f"{r['sync_lag_min']}m" if r["sync_lag_min"] is not None else "—"
        if r["sync_lag_min"] and r["sync_lag_min"] > 120:
            lag += " ⚠️"
        pending = r["pending"] if r["pending"] is not None else "—"
        drift = r["drift_ms"] if r["drift_ms"] is not None else "—"
        print(
            f"{mid:<18} {str(r['version'] or '?'):<8} "
            f"{str(r['cpu_pct'] or 0):<6} {str(r['ram_mb'] or 0):<8} "
            f"{str(r['db_mb'] or 0):<8} {str(pending):<9} "
            f"{lag:<10} {str(drift):<10} {r['rate_per_min'] or 0}"
        )


# ── etl ───────────────────────────────────────────────────────────────────────

def cmd_etl(_args):
    import subprocess
    result = subprocess.run(
        ["docker", "compose", "exec", "etl", "python", "/app/etl/load_events.py"],
        cwd=pathlib.Path(__file__).parent,
    )
    sys.exit(result.returncode)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(prog="manage.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # status
    p = sub.add_parser("status")
    p.add_argument("machine_id", nargs="?")

    # restart
    p = sub.add_parser("restart")
    p.add_argument("machine_id", nargs="?")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--all", action="store_true")

    # logs
    p = sub.add_parser("logs")
    p.add_argument("machine_id")
    p.add_argument("--errors", action="store_true")
    p.add_argument("--tail", type=int, default=20)

    # ack
    p = sub.add_parser("ack")
    p.add_argument("machine_id")

    # update-config
    p = sub.add_parser("update-config")
    p.add_argument("machine_id")
    p.add_argument("--set", nargs="+", metavar="KEY=VALUE", required=True)

    # perf
    p = sub.add_parser("perf")
    p.add_argument("machine_id", nargs="?")
    p.add_argument("--warnings", action="store_true")

    # etl
    sub.add_parser("etl")

    args = parser.parse_args()
    {
        "status":        cmd_status,
        "restart":       cmd_restart,
        "logs":          cmd_logs,
        "ack":           cmd_ack,
        "update-config": cmd_update_config,
        "perf":          cmd_perf,
        "etl":           cmd_etl,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
