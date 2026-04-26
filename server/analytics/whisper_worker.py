import asyncio
import io
import logging
import os
import tempfile
import time
import uuid

import asyncpg
from minio.error import S3Error

from utils import minio_client

log = logging.getLogger(__name__)

WHISPER_MODEL  = os.environ.get("WHISPER_MODEL", "base")
AUDIO_BUCKET   = "windiag-audio"

# Speaker labels per channel
_SPEAKERS = {"mic": "Сотрудник", "loopback": "Собеседники"}


def _load_audio(minio: Minio, path: str) -> bytes | None:
    try:
        response = minio.get_object(AUDIO_BUCKET, path)
        return response.read()
    except S3Error as e:
        if e.code == "NoSuchKey":
            return None
        raise


def _transcribe_channel(audio_bytes: bytes, language: str | None) -> tuple[str | None, list[dict]]:
    """Transcribe audio bytes with faster-whisper. Returns (language, segments)."""
    from faster_whisper import WhisperModel
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")

    with tempfile.NamedTemporaryFile(suffix=".opus", delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name

    try:
        segments_iter, info = model.transcribe(
            tmp_path,
            language=language,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        detected_lang = info.language
        segments = [
            {
                "start": round(s.start, 2),
                "end":   round(s.end, 2),
                "text":  s.text.strip(),
            }
            for s in segments_iter
            if s.text.strip()
        ]
    finally:
        os.unlink(tmp_path)

    return detected_lang, segments


def _merge_channels(
    mic_segments: list[dict],
    loopback_segments: list[dict],
) -> list[dict]:
    """Merge two channel segment lists by timestamp, adding speaker label."""
    combined = (
        [{"speaker": _SPEAKERS["mic"],      **s} for s in mic_segments] +
        [{"speaker": _SPEAKERS["loopback"], **s} for s in loopback_segments]
    )
    combined.sort(key=lambda s: s["start"])
    return combined


def _segments_to_text(segments: list[dict]) -> str:
    lines = []
    for s in segments:
        speaker = s.get("speaker", "")
        ts = f"[{_fmt_ts(s['start'])}]"
        prefix = f"{speaker} {ts}: " if speaker else f"{ts} "
        lines.append(prefix + s["text"])
    return "\n".join(lines)


def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


async def process_meetings_batch(
    pool: asyncpg.Pool,
    batch_size: int = 5,
) -> int:
    """Transcribe pending meetings. Returns count processed."""
    minio = minio_client()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT meeting_id, machine_id, mic_path, loopback_path, duration_sec
            FROM meeting_recordings
            WHERE whisper_done    = FALSE
              AND whisper_skipped = FALSE
              AND (mic_path IS NOT NULL OR loopback_path IS NOT NULL)
            ORDER BY started_at
            LIMIT $1
            """,
            batch_size,
        )

    if not rows:
        log.info("whisper: no meetings to process")
        return 0

    processed = 0
    for row in rows:
        meeting_id = str(row["meeting_id"])
        run_id     = str(uuid.uuid4())
        t0         = time.monotonic()

        try:
            all_segments: list[dict] = []
            detected_lang: str | None = None

            for channel in ("mic", "loopback"):
                path = row["mic_path"] if channel == "mic" else row["loopback_path"]
                if not path:
                    continue

                audio = await asyncio.to_thread(_load_audio, minio, path)
                if audio is None:
                    log.warning("whisper: audio not found in MinIO: %s", path)
                    continue

                lang, segs = await asyncio.to_thread(
                    _transcribe_channel, audio, detected_lang
                )
                detected_lang = detected_lang or lang
                for s in segs:
                    s["speaker"] = _SPEAKERS[channel]
                all_segments.extend(segs)

            if not all_segments:
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE meeting_recordings SET whisper_skipped=TRUE WHERE meeting_id=$1::UUID",
                        meeting_id,
                    )
                log.warning("whisper: no audio loaded for meeting %s, skipped", meeting_id)
                continue

            merged   = _merge_channels(
                [s for s in all_segments if s.get("speaker") == _SPEAKERS["mic"]],
                [s for s in all_segments if s.get("speaker") == _SPEAKERS["loopback"]],
            )
            transcript = _segments_to_text(merged)

            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO meeting_transcripts
                        (meeting_id, run_id, whisper_model, language, transcript, segments)
                    VALUES ($1::UUID, $2::UUID, $3, $4, $5, $6::JSONB)
                    ON CONFLICT (meeting_id, run_id) DO NOTHING
                    """,
                    meeting_id, run_id, WHISPER_MODEL, detected_lang,
                    transcript, __import__("json").dumps(merged),
                )
                await conn.execute(
                    "UPDATE meeting_recordings SET whisper_done=TRUE WHERE meeting_id=$1::UUID",
                    meeting_id,
                )

            elapsed = round(time.monotonic() - t0, 1)
            log.info(
                "whisper: meeting=%s lang=%s segments=%d elapsed=%.1fs",
                meeting_id, detected_lang, len(merged), elapsed,
            )
            processed += 1

        except Exception as e:
            log.error("whisper: meeting=%s error=%s", meeting_id, e, exc_info=True)

    log.info("whisper: processed %d/%d meetings", processed, len(rows))
    return processed
