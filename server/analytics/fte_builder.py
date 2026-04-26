import logging
import time
from datetime import date, timedelta

import asyncpg

log = logging.getLogger(__name__)

# Gloria Mark CHI 2008: context-switching recovery time by cognitive demand
_SWITCH_OVERHEAD_MIN = {"LOW": 1.0, "MEDIUM": 2.5, "HIGH": 5.0}

_WORKDAY_MIN = 480  # 8 hours


def _automation_type(score_normalized: float) -> str:
    if score_normalized >= 0.7:
        return "RPA"
    if score_normalized >= 0.3:
        return "Hybrid"
    return "AI-Agent"


async def build_fte_report(
    pool: asyncpg.Pool,
    lookback_days: int = 30,
    report_date: date | None = None,
) -> int:
    """Aggregate task_sessions into fte_report. Returns count of rows written."""
    if report_date is None:
        report_date = date.today()

    cutoff_ms = int((time.time() - lookback_days * 86_400) * 1000)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                user_id, machine_id, task_label,
                start_ts, end_ts, duration_min,
                has_undo, has_error,
                event_sequence, avg_cognitive_demand
            FROM task_sessions
            WHERE start_ts >= $1
            ORDER BY user_id, machine_id, task_label, start_ts
            """,
            cutoff_ms,
        )

    if not rows:
        log.info("build_fte_report: no task_sessions in lookback window")
        return 0

    # Group by (user_id, machine_id, task_label)
    groups: dict[tuple, list] = {}
    for r in rows:
        key = (r["user_id"], r["machine_id"], r["task_label"])
        groups.setdefault(key, []).append(r)

    # Determine actual date range covered (may be shorter than lookback_days)
    all_ts = [r["start_ts"] for r in rows]
    span_days = max(
        1,
        round((max(all_ts) - min(all_ts)) / 86_400_000) + 1,
    )

    # Collect raw scores for min-max normalisation
    raw_scores: list[tuple[tuple, float]] = []
    stats: dict[tuple, dict] = {}

    for key, sessions in groups.items():
        executions = len(sessions)
        executions_per_day = executions / span_days
        avg_duration = sum(s["duration_min"] for s in sessions) / executions

        sequences = [s["event_sequence"] for s in sessions if s["event_sequence"]]
        unique_seq = len(set(sequences)) if sequences else 1
        repeatability = 1.0 - (unique_seq / max(executions, 1))

        exception_count = sum(
            1 for s in sessions if s["has_undo"] or s["has_error"]
        )
        exception_rate = exception_count / executions

        demands = [s["avg_cognitive_demand"] for s in sessions if s["avg_cognitive_demand"]]
        avg_demand = _mode(demands) if demands else "MEDIUM"
        switch_overhead = _SWITCH_OVERHEAD_MIN.get(avg_demand, 2.5)

        # Effective duration including switching cost
        effective_duration = avg_duration + switch_overhead * exception_rate

        raw = (
            executions_per_day
            * effective_duration
            * repeatability
            / max(exception_rate, 0.01)
        )

        fte = round(avg_duration * executions_per_day / _WORKDAY_MIN, 4)

        stats[key] = {
            "executions_per_day": round(executions_per_day, 4),
            "avg_duration_min": round(avg_duration, 3),
            "repeatability": round(repeatability, 4),
            "exception_rate": round(exception_rate, 4),
            "fte_saving": fte,
            "avg_cognitive_demand": avg_demand,
        }
        raw_scores.append((key, raw))

    # Min-max normalise automation_score
    raw_values = [v for _, v in raw_scores]
    score_min = min(raw_values)
    score_max = max(raw_values)
    score_range = score_max - score_min if score_max > score_min else 1.0

    report_rows = []
    for key, raw in raw_scores:
        normalized = (raw - score_min) / score_range
        s = stats[key]
        report_rows.append((
            report_date,
            key[0], key[1], key[2],
            s["executions_per_day"],
            s["avg_duration_min"],
            round(s["executions_per_day"] * s["avg_duration_min"] / _WORKDAY_MIN * 100, 2),
            s["repeatability"],
            s["exception_rate"],
            round(normalized, 4),
            _automation_type(normalized),
            s["fte_saving"],
            s["avg_cognitive_demand"],
        ))

    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO fte_report (
                report_date, user_id, machine_id, task_label,
                executions_per_day, avg_duration_min, pct_workday,
                repeatability, exception_rate,
                automation_score, automation_type, fte_saving,
                avg_cognitive_demand
            ) VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
            )
            ON CONFLICT (report_date, user_id, machine_id, task_label)
            DO UPDATE SET
                executions_per_day  = EXCLUDED.executions_per_day,
                avg_duration_min    = EXCLUDED.avg_duration_min,
                pct_workday         = EXCLUDED.pct_workday,
                repeatability       = EXCLUDED.repeatability,
                exception_rate      = EXCLUDED.exception_rate,
                automation_score    = EXCLUDED.automation_score,
                automation_type     = EXCLUDED.automation_type,
                fte_saving          = EXCLUDED.fte_saving,
                avg_cognitive_demand = EXCLUDED.avg_cognitive_demand
            """,
            report_rows,
        )

    log.info(
        "build_fte_report: wrote %d rows for %s (lookback %dd, span %dd)",
        len(report_rows), report_date, lookback_days, span_days,
    )
    return len(report_rows)


def _mode(values: list[str]) -> str:
    from collections import Counter
    return Counter(values).most_common(1)[0][0]
