import asyncio
import base64
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Optional

import anthropic
import asyncpg
from minio import Minio
from minio.error import S3Error

from utils import parse_json_response

log = logging.getLogger(__name__)

PROMPT_VERSION = "1.0.0"
MODEL = "claude-sonnet-4-6"
BUCKET = "windiag-screenshots"

SYSTEM_PROMPT = """\
You analyze desktop screenshots from employee workstations to reconstruct \
what business task the user was performing.

For each screenshot return a JSON object with exactly these fields in order:

  screenshot_index: int

  reasoning: string
    ALWAYS include. Observe step by step:
    1. What application and screen/form is visible?
    2. What UI elements, fields, buttons are present?
    3. What does the window title indicate?
    4. What action was the user performing?
    Then state your conclusion.

  task_label: string
    Business task name. Specific and meaningful, not the app name.
    Use the same language as the UI text.
    Good: "Processing incoming invoice", "Reviewing purchase order approval",
          "Filling monthly expense report", "Responding to customer inquiry"
    Bad:  "Using software", "Working in browser", "Viewing document"

  app_context: string
    Application name and screen or form name.

  action_type: DATA_ENTRY | NAVIGATION | SEARCH | APPROVAL | REPORT

  visible_fields: string[]
    Labels of visible form fields. Empty array if none.

  case_id_candidate: string | null
    Document ID, order number, case number, ticket ID visible on screen.
    Null if not visible.

  completion_signal: bool
    True if screen shows a completed action: save confirmation,
    submitted form, approval granted, document printed or exported.

  cognitive_demand: LOW | MEDIUM | HIGH
    LOW: mechanical repetitive input
    MEDIUM: requires judgment or cross-referencing
    HIGH: complex analysis, exception handling, multi-step reasoning

  automation_notes: string
    One sentence on automation potential for this specific task.

  confidence: float 0.0-1.0

Return a JSON array, one object per screenshot, indexed by screenshot_index.\
"""


@dataclass
class _EventRow:
    event_id: str
    machine_id: str
    screenshot_path: str
    session_id: str
    synced_ts: int
    window_title: Optional[str]
    process_name: Optional[str]
    user_id: str
    screenshot_dhash: Optional[int]
    capture_reason: Optional[str]


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


async def _mark_skipped(conn: asyncpg.Connection, event_id: str) -> None:
    await conn.execute(
        "UPDATE events SET vision_skipped = TRUE WHERE event_id = $1", event_id
    )


async def _load_batch(conn: asyncpg.Connection, batch_size: int) -> list[_EventRow]:
    rows = await conn.fetch(
        """
        SELECT event_id, machine_id, screenshot_path, session_id,
               synced_ts, window_title, process_name, user_id,
               screenshot_dhash, capture_reason
        FROM events
        WHERE layer = 'visual'
          AND vision_done = FALSE
          AND vision_skipped = FALSE
          AND screenshot_path IS NOT NULL
        ORDER BY synced_ts
        LIMIT $1
        """,
        batch_size,
    )
    return [_EventRow(**dict(r)) for r in rows]


async def _dhash_filter(
    conn: asyncpg.Connection, event: _EventRow
) -> bool:
    """Return True if the screenshot should be skipped as a duplicate."""
    if event.capture_reason == "ui_event":
        return False
    if not event.screenshot_dhash:
        return False
    prev = await conn.fetchval(
        """
        SELECT screenshot_dhash FROM events
        WHERE session_id = $1
          AND synced_ts < $2
          AND screenshot_dhash IS NOT NULL
        ORDER BY synced_ts DESC
        LIMIT 1
        """,
        event.session_id,
        event.synced_ts,
    )
    if prev is not None and _hamming(event.screenshot_dhash, prev) < 10:
        return True
    return False


def _load_from_minio(minio_client: Minio, path: str) -> Optional[bytes]:
    try:
        response = minio_client.get_object(BUCKET, path.replace("\\", "/"))
        return response.read()
    except S3Error as e:
        if e.code == "NoSuchKey":
            return None
        raise


def _parse_response(text: str, expected_count: int) -> list[dict]:
    parsed = parse_json_response(text)
    if not isinstance(parsed, list):
        raise ValueError("Expected JSON array")
    return parsed


async def _write_vision_results(
    conn: asyncpg.Connection,
    run_id: str,
    event_ids: list[str],
    results: list[dict],
) -> None:
    for item in results:
        idx = item.get("screenshot_index", 1) - 1
        if idx < 0 or idx >= len(event_ids):
            continue
        event_id = event_ids[idx]
        await conn.execute(
            """
            INSERT INTO vision_results (
                event_id, run_id, model, prompt_version, reasoning,
                task_label, app_context, action_type, visible_fields,
                case_id_candidate, completion_signal, cognitive_demand,
                automation_notes, confidence
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
            ON CONFLICT (event_id, run_id) DO NOTHING
            """,
            event_id, run_id, MODEL, PROMPT_VERSION,
            item.get("reasoning"),
            item.get("task_label"),
            item.get("app_context"),
            item.get("action_type"),
            item.get("visible_fields") or [],
            item.get("case_id_candidate"),
            item.get("completion_signal"),
            item.get("cognitive_demand"),
            item.get("automation_notes"),
            float(item["confidence"]) if item.get("confidence") is not None else None,
        )


async def _materialize_to_events(
    conn: asyncpg.Connection, event_ids: list[str]
) -> None:
    await conn.execute(
        """
        UPDATE events e
        SET vision_done         = TRUE,
            vision_task_label   = vr.task_label,
            vision_app_context  = vr.app_context,
            vision_action_type  = vr.action_type,
            vision_case_id      = vr.case_id_candidate,
            vision_is_commit    = vr.completion_signal,
            vision_cognitive    = vr.cognitive_demand,
            vision_confidence   = vr.confidence,
            vision_auto_notes   = vr.automation_notes
        FROM (
            SELECT DISTINCT ON (event_id)
                event_id, task_label, app_context, action_type,
                case_id_candidate, completion_signal, cognitive_demand,
                confidence, automation_notes
            FROM vision_results
            WHERE event_id = ANY($1)
            ORDER BY event_id, confidence DESC NULLS LAST
        ) vr
        WHERE e.event_id = vr.event_id
          AND (e.vision_confidence IS NULL OR vr.confidence > e.vision_confidence)
        """,
        event_ids,
    )


async def process_vision_batch(
    pool: asyncpg.Pool,
    minio_client: Minio,
    batch_size: int = 20,
) -> int:
    """Process one batch of screenshots. Returns count of processed events."""
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    run_id = str(uuid.uuid4())

    async with pool.acquire() as conn:
        events = await _load_batch(conn, batch_size)
        if not events:
            return 0

        # dHash filter
        to_process: list[_EventRow] = []
        for event in events:
            if await _dhash_filter(conn, event):
                await _mark_skipped(conn, event.event_id)
                log.debug("skipped dhash duplicate: %s", event.event_id)
            else:
                to_process.append(event)

        if not to_process:
            return 0

        # Load images from MinIO (sync in thread)
        images: list[Optional[bytes]] = []
        for event in to_process:
            data = await asyncio.to_thread(_load_from_minio, minio_client, event.screenshot_path)
            if data is None:
                await _mark_skipped(conn, event.event_id)
                log.warning("screenshot not found in MinIO: %s", event.screenshot_path)
                images.append(None)
            else:
                images.append(data)

        # Keep only events with images
        valid: list[tuple[_EventRow, bytes]] = [
            (e, img) for e, img in zip(to_process, images) if img is not None
        ]
        if not valid:
            return 0

        # Build messages
        user_content = []
        for i, (event, img_data) in enumerate(valid):
            user_content.append({
                "type": "text",
                "text": (
                    f"Screenshot {i + 1}/{len(valid)}. "
                    f"Process: {event.process_name or 'unknown'}. "
                    f"Window: {event.window_title or 'unknown'}"
                ),
            })
            user_content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/webp",
                    "data": base64.b64encode(img_data).decode(),
                },
            })

        # Call Claude with exponential backoff on rate limit
        response_text: Optional[str] = None
        for attempt in range(3):
            try:
                response = await client.messages.create(
                    model=MODEL,
                    max_tokens=4096,
                    system=[
                        {
                            "type": "text",
                            "text": SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=[{"role": "user", "content": user_content}],
                )
                response_text = response.content[0].text
                break
            except anthropic.RateLimitError:
                wait = 2 ** (attempt + 1)
                log.warning("rate limit, retrying in %ds (attempt %d/3)", wait, attempt + 1)
                await asyncio.sleep(wait)
            except Exception as e:
                log.error("Claude API error: %s", e)
                break

        event_ids = [e.event_id for e, _ in valid]

        if response_text is None:
            log.warning("skipping batch of %d — no API response", len(valid))
            return 0

        # Parse response
        try:
            results = _parse_response(response_text, len(valid))
        except Exception as e:
            log.error("failed to parse Claude response: %s", e)
            # Insert parse_error rows so events are marked done and leave queue
            results = [
                {
                    "screenshot_index": i + 1,
                    "reasoning": None,
                    "task_label": "parse_error",
                    "app_context": None,
                    "action_type": None,
                    "visible_fields": [],
                    "case_id_candidate": None,
                    "completion_signal": None,
                    "cognitive_demand": None,
                    "automation_notes": None,
                    "confidence": 0.0,
                }
                for i in range(len(valid))
            ]

        await _write_vision_results(conn, run_id, event_ids, results)
        await _materialize_to_events(conn, event_ids)

        log.info(
            "vision batch done: run=%s processed=%d skipped=%d",
            run_id, len(valid), len(to_process) - len(valid),
        )
        return len(valid)
