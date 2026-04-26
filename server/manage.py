#!/usr/bin/env python3
"""
manage.py — CLI for Task Mining Agent server.

Usage:
  python manage.py status
  python manage.py status <machine_id>
  python manage.py layers [<machine_id>]
  python manage.py restart <machine_id>
  python manage.py restart --offline
  python manage.py restart --all
  python manage.py restart-layer <machine_id> <layer>
  python manage.py logs <machine_id> [--errors] [--tail N]
  python manage.py ack <machine_id>
  python manage.py update-config <machine_id> --set KEY=VALUE
  python manage.py perf [<machine_id>] [--warnings]
  python manage.py deploy
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


# ── layers ────────────────────────────────────────────────────────────────────

LAYER_ICON = {"ok": "🟢", "stuck": "🔴", "inactive": "⚪", "error": "🟠"}
KNOWN_LAYERS = ["window", "visual", "system", "applogs", "browser", "agent"]


def cmd_layers(args):
    """Show per-layer health for each machine from the last HeartbeatPulse."""
    conn = connect()
    with conn.cursor() as cur:
        query = """
            SELECT DISTINCT ON (machine_id)
                machine_id,
                synced_ts,
                payload->'LayerStats' AS layer_stats
            FROM events
            WHERE event_type = 'HeartbeatPulse'
              AND payload->'LayerStats' IS NOT NULL
        """
        if args.machine_id:
            cur.execute(query + " AND machine_id LIKE %s ORDER BY machine_id, synced_ts DESC",
                        (args.machine_id + "%",))
        else:
            cur.execute(query + " ORDER BY machine_id, synced_ts DESC")
        rows = cur.fetchall()
    conn.close()

    if not rows:
        print("No heartbeat with layer stats found. Agent version may be older than v1.1.5.")
        return

    # Header
    layer_cols = "  ".join(f"{l:<9}" for l in KNOWN_LAYERS)
    print(f"{'MACHINE':<18} {'HB_AGO':<8}  {layer_cols}")
    print("-" * (18 + 8 + 4 + len(KNOWN_LAYERS) * 11))

    for r in rows:
        mid   = (r["machine_id"] or "")[:16]
        stats = r["layer_stats"] or {}
        now_s = datetime.now(timezone.utc).timestamp()
        hb_s  = r["synced_ts"] / 1000
        hb_ago = fmt_duration(int(now_s - hb_s))

        cells = []
        for layer in KNOWN_LAYERS:
            s = stats.get(layer) or {}
            status = s.get("Status", "inactive")
            ev5    = s.get("Events5Min", 0)
            last_s = s.get("LastEventSec", -1)
            icon   = LAYER_ICON.get(status, "?")
            # Show events/5min in parentheses if layer is active
            if status == "ok" and ev5:
                cell = f"{icon}{ev5:<3}ev"
            elif status == "stuck":
                cell = f"{icon}STUCK"
            elif status == "inactive":
                cell = f"{icon}—    "
            else:
                cell = f"{icon}{status}"
            cells.append(f"{cell:<9}")

        print(f"{mid:<18} {hb_ago:<8}  {'  '.join(cells)}")

    # Legend
    print()
    print("Legend: 🟢=ok  🔴=stuck  ⚪=inactive  🟠=error  (N ev = events in last 5 min)")


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
        cid = write_command(mid, "restart_agent")
        print(f"  → restart queued for {mid[:16]}... (command_id={cid[:8]}...)")


def cmd_restart_layer(args):
    cid = write_command(args.machine_id, "restart_layer", {"layer": args.layer})
    print(f"  → restart_layer '{args.layer}' queued for {args.machine_id[:16]}... "
          f"(command_id={cid[:8]}...)")
    print("  Note: individual layer restart triggers a full process restart.")


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


# ── main ──────────────────────────────────────────────────────────────────────

# ── bootstrap ─────────────────────────────────────────────────────────────────

def cmd_deploy(_args):
    import subprocess, pathlib
    root = pathlib.Path(__file__).parent.parent
    compose_dir = pathlib.Path(__file__).parent
    for cmd in [
        ["git", "-C", str(root), "pull"],
        ["docker", "compose", "-f", str(compose_dir / "docker-compose.yml"), "build", "--no-cache"],
        ["docker", "compose", "-f", str(compose_dir / "docker-compose.yml"), "up", "-d"],
    ]:
        print("$", " ".join(cmd))
        subprocess.run(cmd, check=True)


def cmd_bootstrap(args):
    sub = args.bootstrap_sub

    if sub == "generate":
        _bootstrap_generate(args)
    elif sub == "approve":
        _bootstrap_approve(args)
    elif sub == "publish":
        _bootstrap_publish(args)
    elif sub == "revoke":
        _bootstrap_revoke(args)
    elif sub == "status":
        _bootstrap_status(args)
    elif sub == "export-pubkey":
        _bootstrap_export_pubkey()


def _bootstrap_generate(args):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent / "api"))
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from bootstrap.scanner import build_context
    from bootstrap.generator import generate_profile
    from bootstrap.crypto import sign_profile

    server_url = args.server_url or os.environ.get("SERVER_URL", "")
    tenant_id  = args.tenant_id  or os.environ.get("TENANT_ID", "default")
    site_id    = args.site_id    or "default"

    ctx    = build_context(server_url=server_url, tenant_id=tenant_id, site_id=site_id)
    signed = generate_profile(ctx)

    conn = connect()
    with conn.cursor() as cur:
        profile = signed.get_profile()
        cur.execute(
            """
            INSERT INTO bootstrap_profiles
                (tenant_id, site_id, expires_at, status, signed_data, signature, deployment_context)
            VALUES (%s, %s, %s::TIMESTAMPTZ, 'pending', %s, %s, %s::JSONB)
            RETURNING profile_id::TEXT
            """,
            (profile.tenant_id, profile.site_id, profile.expires_at,
             signed.signed_data, signed.signature, ctx.model_dump_json()),
        )
        row = cur.fetchone()
        # Store enrollment token
        cur.execute(
            "INSERT INTO enrollment_tokens (token, profile_id, expires_at) VALUES (%s, %s::UUID, %s::TIMESTAMPTZ)",
            (profile.enrollment.token, row["profile_id"], profile.enrollment.expires_at),
        )
    conn.commit()
    print(f"Profile generated: {row['profile_id']}")
    print(f"Status : pending")
    print(f"Context: AD={ctx.has_ad} L2={ctx.l2_reachable} internet={ctx.has_internet}")
    print(f"Server : {profile.endpoints.primary}")
    print(f"Expires: {profile.expires_at}")
    print()
    print("Next: python manage.py bootstrap approve " + row["profile_id"])


def _bootstrap_approve(args):
    conn = connect()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE bootstrap_profiles SET status='approved' WHERE profile_id=%s::UUID AND status='pending' RETURNING profile_id",
            (args.profile_id,),
        )
        if not cur.fetchone():
            print("Profile not found or not in pending state")
            return
    conn.commit()
    print(f"Profile {args.profile_id[:8]}... approved")
    print(f"Next: python manage.py bootstrap publish {args.profile_id}")


def _bootstrap_publish(args):
    conn = connect()
    with conn.cursor() as cur:
        # Expire any currently active profiles
        cur.execute(
            "UPDATE bootstrap_profiles SET status='expired' WHERE status IN ('published','active') AND profile_id != %s::UUID",
            (args.profile_id,),
        )
        cur.execute(
            "UPDATE bootstrap_profiles SET status='active' WHERE profile_id=%s::UUID AND status='approved' RETURNING profile_id",
            (args.profile_id,),
        )
        if not cur.fetchone():
            print("Profile not found or not in approved state")
            return
    conn.commit()
    print(f"Profile {args.profile_id[:8]}... is now ACTIVE")
    print("Agents will receive this profile on next bootstrap resolution.")


def _bootstrap_revoke(args):
    conn = connect()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE bootstrap_profiles SET status='revoked' WHERE profile_id=%s::UUID",
            (args.profile_id,),
        )
    conn.commit()
    print(f"Profile {args.profile_id[:8]}... revoked")


def _bootstrap_status(args):
    conn = connect()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT profile_id::TEXT, tenant_id, site_id, status, expires_at, created_at FROM bootstrap_profiles ORDER BY created_at DESC LIMIT 10"
        )
        rows = cur.fetchall()
    if not rows:
        print("No bootstrap profiles found.")
        return
    print(f"{'PROFILE_ID':<38} {'STATUS':<10} {'EXPIRES':<25} {'TENANT'}")
    print("-" * 90)
    for r in rows:
        print(f"{r['profile_id']:<38} {r['status']:<10} {str(r['expires_at'])[:19]:<25} {r['tenant_id']}")


def _bootstrap_export_pubkey():
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent / "api"))
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from bootstrap.crypto import export_public_key_pem
    print(export_public_key_pem())
    print()
    print("# Embed the PEM above in src/WinDiagSvc/Bootstrap/ProfileVerifier.cs")
    print("# Replace the REPLACE_WITH_SERVER_CA_PUBLIC_KEY_PEM placeholder.")


def main():
    parser = argparse.ArgumentParser(prog="manage.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # status
    p = sub.add_parser("status")
    p.add_argument("machine_id", nargs="?")

    # layers
    p = sub.add_parser("layers")
    p.add_argument("machine_id", nargs="?")

    # restart
    p = sub.add_parser("restart")
    p.add_argument("machine_id", nargs="?")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--all", action="store_true")

    # restart-layer
    p = sub.add_parser("restart-layer")
    p.add_argument("machine_id")
    p.add_argument("layer", choices=["window", "visual", "system", "applogs", "browser", "agent"])

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

    # deploy
    sub.add_parser("deploy")

    # bootstrap
    p = sub.add_parser("bootstrap")
    bs = p.add_subparsers(dest="bootstrap_sub", required=True)
    g = bs.add_parser("generate")
    g.add_argument("--server-url", default="")
    g.add_argument("--tenant-id",  default="")
    g.add_argument("--site-id",    default="default")
    ap = bs.add_parser("approve")
    ap.add_argument("profile_id")
    pb = bs.add_parser("publish")
    pb.add_argument("profile_id")
    rv = bs.add_parser("revoke")
    rv.add_argument("profile_id")
    bs.add_parser("status")
    bs.add_parser("export-pubkey")

    args = parser.parse_args()
    {
        "status":         cmd_status,
        "layers":         cmd_layers,
        "restart":        cmd_restart,
        "restart-layer":  cmd_restart_layer,
        "logs":           cmd_logs,
        "ack":            cmd_ack,
        "update-config":  cmd_update_config,
        "perf":           cmd_perf,
        "bootstrap":      cmd_bootstrap,
        "deploy":         cmd_deploy,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
