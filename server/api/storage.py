import io
import os
import asyncio
from minio import Minio
from minio.error import S3Error

BUCKET_SCREENSHOTS = "windiag-screenshots"
BUCKET_AUDIO       = "windiag-audio"


def _make_client() -> Minio:
    return Minio(
        endpoint=os.environ.get("MINIO_ENDPOINT", "localhost:9000"),
        access_key=os.environ["MINIO_ACCESS_KEY"],
        secret_key=os.environ["MINIO_SECRET_KEY"],
        secure=False,
    )


def _ensure_bucket_sync(client: Minio, bucket: str):
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)


async def ensure_bucket():
    import logging
    try:
        client = _make_client()
        await asyncio.to_thread(_ensure_bucket_sync, client, BUCKET_SCREENSHOTS)
    except Exception as e:
        logging.getLogger(__name__).warning("ensure_bucket screenshots failed: %s", e)


async def ensure_audio_bucket():
    import logging
    try:
        client = _make_client()
        await asyncio.to_thread(_ensure_bucket_sync, client, BUCKET_AUDIO)
    except Exception as e:
        logging.getLogger(__name__).warning("ensure_bucket audio failed: %s", e)


def _put_sync(client: Minio, bucket: str, key: str, data: bytes, content_type: str):
    client.put_object(bucket, key, io.BytesIO(data), len(data), content_type=content_type)


def _get_sync(client: Minio, bucket: str, key: str) -> bytes:
    response = client.get_object(bucket, key)
    return response.read()


async def put_screenshot(object_key: str, data: bytes):
    client = _make_client()
    await asyncio.to_thread(_put_sync, client, BUCKET_SCREENSHOTS, object_key, data, "image/webp")


def screenshot_key(machine_id: str, date_str: str, event_id: str) -> str:
    return f"{machine_id}/{date_str}/{event_id}.webp"


async def put_audio(object_key: str, data: bytes):
    client = _make_client()
    await asyncio.to_thread(_put_sync, client, BUCKET_AUDIO, object_key, data, "audio/ogg")


async def get_audio(object_key: str) -> bytes:
    client = _make_client()
    return await asyncio.to_thread(_get_sync, client, BUCKET_AUDIO, object_key)


def audio_key(machine_id: str, meeting_id: str, channel: str) -> str:
    return f"{machine_id}/{meeting_id}/{channel}.opus"
