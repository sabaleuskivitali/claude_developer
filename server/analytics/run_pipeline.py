"""
Pipeline coordinator. Run modes:
  vision       — process one batch of screenshots via Claude Vision
  reconstruct  — segment vision-done events into task_sessions
  fte          — aggregate task_sessions into fte_report
  meetings     — transcribe meetings (whisper) + summarize (Claude)
  all          — vision + reconstruct + fte + meetings

Usage:
  python run_pipeline.py [vision|reconstruct|fte|meetings|all] [--batch-size N] [--lookback-hours N]
"""
import argparse
import asyncio
import logging
import sys
import time

import asyncpg

from fte_builder import build_fte_report
from meeting_summarizer import summarize_meetings
from task_reconstructor import reconstruct_tasks
from utils import minio_client
from vision_worker import process_vision_batch
from whisper_worker import process_meetings_batch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("pipeline")


async def run_vision(pool: asyncpg.Pool, batch_size: int) -> int:
    minio = minio_client()
    total = 0
    while True:
        n = await process_vision_batch(pool, minio, batch_size)
        total += n
        if n == 0:
            break
        log.info("vision: processed %d this batch, %d total so far", n, total)
    log.info("vision: queue empty, total processed = %d", total)
    return total


async def run_reconstruct(pool: asyncpg.Pool, lookback_hours: int) -> int:
    n = await reconstruct_tasks(pool, lookback_hours)
    log.info("reconstruct: wrote %d task_sessions", n)
    return n


async def run_fte(pool: asyncpg.Pool, lookback_days: int) -> int:
    n = await build_fte_report(pool, lookback_days)
    log.info("fte: wrote %d rows", n)
    return n


async def run_meetings(pool: asyncpg.Pool, batch_size: int) -> int:
    n = await process_meetings_batch(pool, batch_size)
    total = n
    while n > 0:
        n = await process_meetings_batch(pool, batch_size)
        total += n
    n_summ = await summarize_meetings(pool)
    log.info("meetings: transcribed=%d summarized=%d", total, n_summ)
    return total


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=["vision", "reconstruct", "fte", "meetings", "all"],
        nargs="?",
        default="all",
    )
    parser.add_argument("--batch-size",     type=int, default=20)
    parser.add_argument("--lookback-hours", type=int, default=48)
    parser.add_argument("--lookback-days",  type=int, default=30)
    args = parser.parse_args()

    pool = await asyncpg.create_pool(
        os.environ["POSTGRES_DSN"],
        min_size=1,
        max_size=5,
        statement_cache_size=0,
    )

    t0 = time.monotonic()
    try:
        if args.mode in ("vision", "all"):
            await run_vision(pool, args.batch_size)

        if args.mode in ("reconstruct", "all"):
            await run_reconstruct(pool, args.lookback_hours)

        if args.mode in ("fte", "all"):
            await run_fte(pool, args.lookback_days)

        if args.mode in ("meetings", "all"):
            await run_meetings(pool, args.batch_size)

    finally:
        await pool.close()

    elapsed = round(time.monotonic() - t0, 1)
    log.info("pipeline done in %.1fs", elapsed)


if __name__ == "__main__":
    asyncio.run(main())
