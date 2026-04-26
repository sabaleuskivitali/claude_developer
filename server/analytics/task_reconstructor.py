import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import asyncpg

from utils import mode_or_default

log = logging.getLogger(__name__)

# Boundary thresholds (ICPM 2020, arXiv 2510.08118)
_GAP_PRIMARY_MS  = 180_000   # 3 min: confirms completion_signal
_GAP_FALLBACK_MS = 900_000   # 15 min: boundary even without commit

# case_id scoring weights
_WEIGHTS = {"vision_case_id": 10, "window_regex": 7, "mru_document": 4}


@dataclass
class _VisualEvent:
    event_id: str
    session_id: str
    user_id: str
    machine_id: str
    synced_ts: int
    vision_task_label: Optional[str]
    vision_is_commit: Optional[bool]
    process_name: Optional[str]
    window_title: Optional[str]
    vision_case_id: Optional[str]
    case_id: Optional[str]


def _is_boundary(
    prev: _VisualEvent,
    curr: _VisualEvent,
    idle_between: bool,
    gap_ms: int,
) -> bool:
    if prev.session_id != curr.session_id:
        return True
    if prev.vision_is_commit and (idle_between or gap_ms > _GAP_PRIMARY_MS):
        return True
    if gap_ms > _GAP_FALLBACK_MS:
        return True
    return False
    # task_label change without completion_signal → NOT a boundary
    # app switch alone → NOT a boundary


def _score_candidate(
    value: str,
    source: str,
    distance_from_start_ms: int,
    count_in_session: int,
    vision_case_id: Optional[str],
) -> float:
    base      = _WEIGHTS.get(source, 0)
    proximity = max(0.0, 1.0 - distance_from_start_ms / 120_000) * 3
    freq      = min(count_in_session - 1, 3) * 1.5
    cross     = 2.0 if vision_case_id and vision_case_id in value else 0.0
    return base + proximity + freq + cross



async def _build_task_session(
    conn: asyncpg.Connection,
    group: list[_VisualEvent],
    session_events: dict[str, list[dict]],
) -> Optional[dict]:
    if not group:
        return None

    first    = group[0]
    last     = group[-1]
    start_ts = first.synced_ts
    end_ts   = last.synced_ts

    # Prefer most common task_label in the group
    labels = [e.vision_task_label for e in group if e.vision_task_label]
    task_label = mode_or_default(labels, "unknown")

    # avg_cognitive_demand from vision_results for these events
    event_ids = [e.event_id for e in group]
    cog_rows = await conn.fetch(
        """
        SELECT DISTINCT ON (event_id) cognitive_demand
        FROM vision_results
        WHERE event_id = ANY($1)
          AND cognitive_demand IS NOT NULL
        ORDER BY event_id, confidence DESC NULLS LAST
        """,
        event_ids,
    )
    avg_cognitive = mode_or_default(
        [r["cognitive_demand"] for r in cog_rows], "MEDIUM"
    )

    # All events for this session in [start_ts, end_ts]
    all_sess = session_events.get(first.session_id, [])
    window   = [e for e in all_sess if start_ts <= e["synced_ts"] <= end_ts]

    has_undo  = any(e["event_type"] == "HotkeyUndo" for e in window)
    has_error = any(e["event_type"] == "ErrorDialogAppeared" for e in window)

    seq_types = sorted({
        e["event_type"] for e in window
        if e["layer"] in ("window", "visual") and e["event_type"] != "Screenshot"
    })
    event_sequence = (
        hashlib.md5(",".join(seq_types).encode()).hexdigest() if seq_types else None
    )

    process_names = list({e["process_name"] for e in window if e.get("process_name")})

    # case_id scoring: ±120s window
    wide_start = start_ts - 120_000
    wide_end   = end_ts   + 120_000
    wide       = [e for e in all_sess if wide_start <= e["synced_ts"] <= wide_end]

    best_vision = next((e.vision_case_id for e in group if e.vision_case_id), None)
    candidates: list[dict] = []

    # Source 1: vision_case_id
    vision_counts: dict[str, int] = {}
    for e in group:
        if e.vision_case_id:
            vision_counts[e.vision_case_id] = vision_counts.get(e.vision_case_id, 0) + 1
    for value, count in vision_counts.items():
        score = _score_candidate(value, "vision_case_id", 0, count, best_vision)
        candidates.append({
            "value": value, "type": "inferred_id",
            "source": "vision_case_id", "score": round(score, 2),
        })

    # Source 2: regex case_id from window_title
    regex_counts: dict[str, int] = {}
    for e in group:
        if e.case_id:
            regex_counts[e.case_id] = regex_counts.get(e.case_id, 0) + 1
    for value, count in regex_counts.items():
        score = _score_candidate(value, "window_regex", 0, count, best_vision)
        candidates.append({
            "value": value, "type": "inferred_id",
            "source": "window_regex", "score": round(score, 2),
        })

    # Source 3: MRU / LNK document_name
    mru_counts: dict[str, int] = {}
    for e in wide:
        if (
            e["event_type"] in ("RecentDocumentOpened", "LnkCreated")
            and e.get("document_name")
        ):
            v = e["document_name"]
            mru_counts[v] = mru_counts.get(v, 0) + 1
    for value, count in mru_counts.items():
        score = _score_candidate(value, "mru_document", 0, count, best_vision)
        candidates.append({
            "value": value, "type": "document_name",
            "source": "mru_document", "score": round(score, 2),
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    best_case_id = (
        candidates[0]["value"]
        if candidates
        else f"s{first.session_id[:8]}_{start_ts}"
    )

    return {
        "session_id":           str(uuid.uuid4()),
        "user_id":              first.user_id,
        "machine_id":           first.machine_id,
        "task_label":           task_label,
        "start_ts":             start_ts,
        "end_ts":               end_ts,
        "duration_min":         round((end_ts - start_ts) / 60_000, 3),
        "screenshot_count":     len(group),
        "event_count":          len(window),
        "case_id":              best_case_id,
        "case_id_candidates":   candidates or None,
        "process_names":        process_names,
        "has_undo":             has_undo,
        "has_error":            has_error,
        "event_sequence":       event_sequence,
        "avg_cognitive_demand": avg_cognitive,
    }


async def reconstruct_tasks(pool: asyncpg.Pool, lookback_hours: int = 48) -> int:
    """Segment vision-processed events into task_sessions. Returns count written."""
    cutoff_ms = int((time.time() - lookback_hours * 3600) * 1000)

    async with pool.acquire() as conn:
        # Visual events that have been processed by Vision
        visual_rows = await conn.fetch(
            """
            SELECT event_id, session_id, user_id, machine_id,
                   synced_ts, vision_task_label, vision_is_commit,
                   process_name, window_title, vision_case_id, case_id
            FROM events
            WHERE layer = 'visual'
              AND vision_done = TRUE
              AND synced_ts >= $1
            ORDER BY session_id, synced_ts
            """,
            cutoff_ms,
        )

        if not visual_rows:
            log.info("no vision-processed events to reconstruct")
            return 0

        session_ids = list({str(r["session_id"]) for r in visual_rows})

        # All events for these sessions (for metadata + case_id window)
        all_rows = await conn.fetch(
            """
            SELECT session_id, synced_ts, event_type, layer,
                   process_name, document_name
            FROM events
            WHERE session_id = ANY($1)
              AND synced_ts >= $2
            ORDER BY session_id, synced_ts
            """,
            session_ids,
            cutoff_ms - 120_000,  # extend for ±120s case_id window
        )

        # IdleStart timestamps per session
        idle_rows = await conn.fetch(
            """
            SELECT session_id, synced_ts
            FROM events
            WHERE session_id = ANY($1)
              AND event_type = 'IdleStart'
              AND synced_ts >= $2
            """,
            session_ids,
            cutoff_ms,
        )

        # Build in-memory indexes
        session_events: dict[str, list[dict]] = {}
        for r in all_rows:
            sid = str(r["session_id"])
            session_events.setdefault(sid, []).append(dict(r))

        idle_ts: dict[str, list[int]] = {}
        for r in idle_rows:
            idle_ts.setdefault(str(r["session_id"]), []).append(r["synced_ts"])

        # Group visual events by session
        visual_by_session: dict[str, list[_VisualEvent]] = {}
        for r in visual_rows:
            ev = _VisualEvent(
                event_id=str(r["event_id"]),
                session_id=str(r["session_id"]),
                user_id=r["user_id"],
                machine_id=r["machine_id"],
                synced_ts=r["synced_ts"],
                vision_task_label=r["vision_task_label"],
                vision_is_commit=r["vision_is_commit"],
                process_name=r["process_name"],
                window_title=r["window_title"],
                vision_case_id=r["vision_case_id"],
                case_id=r["case_id"],
            )
            visual_by_session.setdefault(str(r["session_id"]), []).append(ev)

        # Segment each agent-session into task_sessions and write
        total = 0
        for session_id, events in visual_by_session.items():
            groups: list[list[_VisualEvent]] = []
            current: list[_VisualEvent] = [events[0]]

            for i in range(1, len(events)):
                prev, curr = events[i - 1], events[i]
                gap_ms = curr.synced_ts - prev.synced_ts
                idle_between = any(
                    prev.synced_ts < ts < curr.synced_ts
                    for ts in idle_ts.get(session_id, [])
                )
                if _is_boundary(prev, curr, idle_between, gap_ms):
                    groups.append(current)
                    current = [curr]
                else:
                    current.append(curr)
            groups.append(current)

            for group in groups:
                ts = await _build_task_session(conn, group, session_events)
                if not ts:
                    continue
                await conn.execute(
                    """
                    INSERT INTO task_sessions (
                        session_id, user_id, machine_id, task_label,
                        start_ts, end_ts, duration_min,
                        screenshot_count, event_count,
                        case_id, case_id_candidates, process_names,
                        has_undo, has_error, event_sequence, avg_cognitive_demand
                    ) VALUES (
                        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16
                    )
                    ON CONFLICT (session_id) DO NOTHING
                    """,
                    ts["session_id"], ts["user_id"], ts["machine_id"],
                    ts["task_label"], ts["start_ts"], ts["end_ts"],
                    ts["duration_min"], ts["screenshot_count"], ts["event_count"],
                    ts["case_id"],
                    json.dumps(ts["case_id_candidates"]) if ts["case_id_candidates"] else None,
                    ts["process_names"],
                    ts["has_undo"], ts["has_error"],
                    ts["event_sequence"], ts["avg_cognitive_demand"],
                )
                total += 1

        log.info("reconstruct_tasks: wrote %d task_sessions", total)
        return total
