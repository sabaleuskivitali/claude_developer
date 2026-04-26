import json
import logging
import os

import anthropic
import asyncpg

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"

_SYSTEM = """\
You analyze transcripts of business meetings recorded on employee workstations.

Given a transcript with speaker labels and timestamps, return a JSON object with exactly these fields:

  language: string
    Language of the transcript (ISO 639-1 code, e.g. "ru", "en")

  summary: string
    2-5 sentence summary of what was discussed and decided.
    Use the same language as the transcript.

  action_items: array of objects
    Each item: {"owner": string|null, "text": string, "due": string|null}
    owner — person responsible (from transcript context), null if unclear
    due   — deadline if mentioned, null otherwise
    Use the same language as the transcript.

  case_id: string|null
    Document number, order number, ticket ID, or project name mentioned.
    Null if not identifiable.

  participants: array of strings
    Names or roles of participants mentioned in the transcript.
    Always include "Сотрудник" (or "Employee") for the mic channel.

  topics: array of strings
    Main topics discussed (3-7 short phrases).

Return only the JSON object, no markdown.\
"""


async def summarize_meetings(pool: asyncpg.Pool) -> int:
    """Add Claude summary to transcribed meetings without one. Returns count updated."""
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT mt.id, mt.meeting_id, mt.transcript, mr.duration_sec
            FROM meeting_transcripts mt
            JOIN meeting_recordings mr ON mr.meeting_id = mt.meeting_id
            WHERE mt.summary IS NULL
              AND mt.transcript IS NOT NULL
              AND length(mt.transcript) > 50
            ORDER BY mt.processed_at
            LIMIT 20
            """,
        )

    if not rows:
        log.info("meeting_summarizer: nothing to summarize")
        return 0

    updated = 0
    for row in rows:
        transcript = row["transcript"]
        duration   = row["duration_sec"] or 0

        # Truncate very long transcripts to ~12k tokens worth
        if len(transcript) > 40_000:
            transcript = transcript[:40_000] + "\n[transcript truncated]"

        prompt = (
            f"Meeting duration: {int(duration // 60)}m {int(duration % 60)}s\n\n"
            f"Transcript:\n{transcript}"
        )

        try:
            response = await client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]

            parsed = json.loads(text)

            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE meeting_transcripts
                    SET summary      = $1,
                        action_items = $2::JSONB,
                        case_id      = $3
                    WHERE id = $4
                    """,
                    parsed.get("summary"),
                    json.dumps(parsed.get("action_items") or []),
                    parsed.get("case_id"),
                    row["id"],
                )
            updated += 1
            log.info("meeting_summarizer: summarized meeting=%s", row["meeting_id"])

        except Exception as e:
            log.error("meeting_summarizer: meeting=%s error=%s", row["meeting_id"], e)

    log.info("meeting_summarizer: updated %d transcripts", updated)
    return updated
