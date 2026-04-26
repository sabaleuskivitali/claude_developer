"""
Pipeline coordinator. Run modes:
  vision       — process one batch of screenshots via Claude Vision
  reconstruct  — segment vision-done events into task_sessions
  fte          — aggregate task_sessions into fte_report
  all          — vision (until queue empty) + reconstruct + fte

Usage:
  python run_pipeline.py [vision|reconstruct|fte|all] [--batch-size N] [--lookback-hours N]
"""
import argparse
import asyncio
import logging
import os
import sys
import time

import asyncpg
from minio import Minio

from fte_builder import build_fte_report
from task_reconstructor import reconstruct_tasks
from vision_worker import process_vision_batch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("pipeline")


def _minio_client() -> Minio:
    return Minio(
        os.environ["MINIO_ENDPOINT"],
        access_key=os.environ["MINIO_ACCESS_KEY"],
        secret_key=os.environ["MINIO_SECRET_KEY"],
        secure=os.environ.get("MINIO_SECURE", "false").lower() == "true",
    )


async def run_vision(pool: asyncpg.Pool, batch_size: int) -> int:
    minio = _minio_client()
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


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=["vision", "reconstruct", "fte", "all"],
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

    finally:
        await pool.close()

    elapsed = round(time.monotonic() - t0, 1)
    log.info("pipeline done in %.1fs", elapsed)


if __name__ == "__main__":
    asyncio.run(main())
