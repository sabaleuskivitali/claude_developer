import os
import asyncio
from minio import Minio
from minio.error import S3Error

BUCKET = "windiag-screenshots"


def _make_client() -> Minio:
    return Minio(
        endpoint=os.environ.get("MINIO_ENDPOINT", "localhost:9000"),
        access_key=os.environ["MINIO_ACCESS_KEY"],
        secret_key=os.environ["MINIO_SECRET_KEY"],
        secure=False,
    )


async def ensure_bucket():
    import logging
    try:
        client = _make_client()
        await asyncio.to_thread(_ensure_bucket_sync, client)
    except Exception as e:
        logging.getLogger(__name__).warning("ensure_bucket failed (screenshots unavailable): %s", e)


def _ensure_bucket_sync(client: Minio):
    if not client.bucket_exists(BUCKET):
        client.make_bucket(BUCKET)


async def put_screenshot(object_key: str, data: bytes):
    client = _make_client()
    await asyncio.to_thread(_put_sync, client, object_key, data)


def _put_sync(client: Minio, object_key: str, data: bytes):
    import io
    client.put_object(BUCKET, object_key, io.BytesIO(data), len(data), content_type="image/webp")


def screenshot_key(machine_id: str, date_str: str, event_id: str) -> str:
    return f"{machine_id}/{date_str}/{event_id}.webp"
